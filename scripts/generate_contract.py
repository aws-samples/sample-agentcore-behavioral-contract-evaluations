#!/usr/bin/env python3
"""Generate a behavioral contract from a known-good AgentCore session.

Run your agent once on a golden case, wait 2-5 minutes for CloudWatch
ingestion, then point this script at the session. It pulls the session's
spans from the aws/spans log group, normalizes them, and generates a
contract YAML that encodes what the agent did as what it MUST do:

- every data source consulted        -> must_retrieve_from
- tools were used                    -> must_not_use_only_parametric_knowledge
- every tool called                  -> must_include_steps
- the order tools were called in     -> must_precede chain
- citations present in the output    -> must_contain_citations
- the agent's service name           -> applies_to routing pattern

Review the generated YAML before deploying: auto-generation encodes one
golden execution; you decide which constraints generalize.

Usage:
    python scripts/generate_contract.py \
        --session-id <SESSION_ID> \
        --name my-agent-v1 \
        --output evaluator/contracts/my_agent.yaml

Env: AWS_PROFILE / AWS_REGION respected; --region overrides.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import boto3

# Reuse the evaluator's span shim so generation sees exactly what validation sees.
_EVALUATOR_DIR = Path(__file__).parent.parent / "evaluator"
sys.path.insert(0, str(_EVALUATOR_DIR))
# Vendored libraries (agent_validator, agentic_otel) ship with the sample.
sys.path.insert(0, str(_EVALUATOR_DIR / "vendor"))
from span_shim import extract_service_names, shim_to_otlp  # noqa: E402

from agent_validator.adapters.otel import OTELAdapter  # noqa: E402
from agent_validator.generate import (  # noqa: E402
    contract_to_yaml,
    generate_contract_from_trace,
    generate_source_map_from_trace,
)


def pull_session_spans(logs, session_id: str, hours: int) -> list[dict]:
    """Fetch all spans for a session from the aws/spans log group.

    Reads log streams directly with get_log_events and filters client-side.
    Time bounds are derived from AWS-side stream timestamps rather than the
    local clock (local clock skew otherwise silently empties the results,
    and span records carry span-time timestamps that make filter_log_events
    and Logs Insights time filters unreliable for this group).
    """
    spans: list[dict] = []

    streams = logs.describe_log_streams(
        logGroupName="aws/spans", orderBy="LastEventTime", descending=True, limit=25
    )["logStreams"]
    if not streams:
        return spans

    # Clock-skew-immune reference: the newest event AWS has seen.
    reference_ms = max((s.get("lastEventTimestamp") or 0) for s in streams)
    start_ms = reference_ms - hours * 3600 * 1000

    for stream in streams:
        last = stream.get("lastEventTimestamp", 0)
        if last and last < start_ms:
            continue
        # No startTime: span records carry span-time timestamps that CloudWatch
        # time filters treat inconsistently. Read the stream and filter locally.
        kwargs = {
            "logGroupName": "aws/spans",
            "logStreamName": stream["logStreamName"],
            "startFromHead": True,
        }
        prev_token = None
        while True:
            resp = logs.get_log_events(**kwargs)
            for ev in resp.get("events", []):
                if session_id not in ev["message"]:
                    continue
                try:
                    span = json.loads(ev["message"])
                except json.JSONDecodeError:
                    continue
                if (span.get("attributes") or {}).get("session.id") == session_id:
                    spans.append(span)
            token = resp.get("nextForwardToken")
            if not token or token == prev_token:
                break
            prev_token = token
            kwargs["nextToken"] = token
    return spans


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--session-id", required=True, help="Golden session to learn from")
    parser.add_argument("--name", required=True, help="Contract name (e.g. my-agent-v1)")
    parser.add_argument("--output", required=True, help="Output YAML path")
    parser.add_argument("--source-map", help="JSON file mapping tool names to data sources "
                        '(e.g. {"lookup_formulary": {"source_name": "formulary-db", "source_type": "database"}}). '
                        "Without it, data-source constraints cannot be inferred.")
    parser.add_argument("--hours", type=int, default=12, help="Lookback window (default 12)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    logs = boto3.Session(region_name=args.region).client("logs")
    print(f"Pulling spans for session {args.session_id}...")
    raw_spans = pull_session_spans(logs, args.session_id, args.hours)
    if not raw_spans:
        print("No spans found. Check the session ID, region, and allow 2-5 minutes "
              "after invocation for CloudWatch ingestion.")
        return 1
    print(f"Found {len(raw_spans)} spans.")

    tool_source_map = {}
    if args.source_map:
        with open(args.source_map) as f:
            tool_source_map = json.load(f)

    otlp = shim_to_otlp(raw_spans)
    adapter = OTELAdapter(tool_source_map=tool_source_map)
    execution = adapter.normalize(otlp)

    print(f"Golden execution: {len(execution.steps)} steps, "
          f"tools: {[s.name for s in execution.tool_calls()]}")

    contract = generate_contract_from_trace(execution, name=args.name)
    source_map = tool_source_map or generate_source_map_from_trace(execution)
    yaml_text = contract_to_yaml(contract, source_map)

    # Add the routing pattern for this agent: the service.name minus the
    # endpoint qualifier ("claimsdemo_claims_agent.DEFAULT" -> "claimsdemo_claims_agent"),
    # wrapped in wildcards so any endpoint of the same runtime matches.
    services = extract_service_names(otlp["spans"])
    patterns = sorted({f"*{svc.split('.')[0]}*" for svc in services if svc})
    routing = "# Routes this contract to the agent(s) whose service name matches.\napplies_to:\n"
    for p in patterns:
        routing += f'  - "{p}"\n'

    lines = yaml_text.splitlines()
    # Insert applies_to after the version line
    for i, line in enumerate(lines):
        if line.startswith("version:"):
            lines[i + 1:i + 1] = ["", *routing.rstrip().splitlines()]
            break
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n")

    print(f"\nContract written to {output}")
    print(f"  constraints: {len(contract.constraints)}")
    print(f"  applies_to:  {patterns}")
    if not execution.output_text:
        print("\nNote: raw aws/spans records do not include message content, so no")
        print("citation constraint was inferred. Add `must_contain_citations` to the")
        print("YAML manually if your agent's output must cite sources.")
    print("\nReview the YAML, then redeploy the evaluator:")
    print("  python scripts/deploy_evaluator_lambda.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
