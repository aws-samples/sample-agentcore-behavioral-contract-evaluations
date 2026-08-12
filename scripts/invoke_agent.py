#!/usr/bin/env python3
"""Invoke the claims demo agent on AgentCore Runtime.

Sends a JSON payload with a compliance mode:
    {"prompt": "...", "mode": "compliant" | "lazy"}

- compliant (default): agent follows the required process -> contract PASS
- lazy: agent answers from parametric knowledge, no tools -> contract FAIL

Usage:
    python scripts/invoke_agent.py --mode both                # demo pair, ARN auto-discovered
    python scripts/invoke_agent.py [--mode lazy] [--prompt "..."]
    python scripts/invoke_agent.py --runtime-arn <arn> ...    # override discovery

The runtime ARN is discovered automatically from the CloudFormation stack the
CDK deploy created (default: AgentCore-claimsdemo-default), falling back to
the AgentCore control plane. Pass --runtime-arn to skip discovery.

Env:
    AWS_PROFILE / AWS_REGION respected. --region overrides.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import boto3

DEFAULT_PROMPT = "Process pharmacy claim for member MEM-001, drug lisinopril, 30-day supply."
DEFAULT_STACK = "AgentCore-claimsdemo-default"
DEFAULT_RUNTIME = "claims_agent"
PROJECT_CONFIG = Path(__file__).parent.parent / "claimsdemo" / "agentcore" / "agentcore.json"


def _expected_runtime_id(runtime_name: str) -> str:
    """The service-side runtime name is `<project>_<runtime_name>`."""
    project = "claimsdemo"
    try:
        project = json.loads(PROJECT_CONFIG.read_text())["name"]
    except Exception:  # noqa: BLE001 -- fall back to the sample's project name
        pass
    return f"{project}_{runtime_name}"


def resolve_runtime_arn(session: boto3.Session, stack_name: str, runtime_name: str) -> str | None:
    """Find a demo runtime ARN by name without the user copy-pasting it.

    Tries the CDK stack's CloudFormation outputs first, then falls back to
    listing runtimes on the AgentCore control plane. Matching is exact on
    `<project>_<runtime_name>` -- substring matching is ambiguous once the
    project has more than one runtime (e.g. claims_agent vs
    langgraph_claims_agent).
    """
    expected = _expected_runtime_id(runtime_name)
    try:
        cfn = session.client("cloudformation")
        outputs = cfn.describe_stacks(StackName=stack_name)["Stacks"][0].get("Outputs", [])
        for output in outputs:
            value = output.get("OutputValue", "")
            if value.startswith("arn:") and ":bedrock-agentcore:" in value and ":runtime/" in value:
                arn = value.split("/runtime-endpoint/")[0]
                # runtime id is `<project>_<runtime_name>-<suffix>`
                runtime_id = arn.rsplit(":runtime/", 1)[1]
                if runtime_id.rsplit("-", 1)[0] == expected:
                    return arn
    except Exception:  # noqa: BLE001 -- stack absent or no CFN access; try the control plane
        pass
    try:
        ctl = session.client("bedrock-agentcore-control")
        token = None
        while True:
            kwargs = {"maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            resp = ctl.list_agent_runtimes(**kwargs)
            for runtime in resp.get("agentRuntimes", []):
                if runtime.get("agentRuntimeName", "") == expected:
                    return runtime["agentRuntimeArn"]
            token = resp.get("nextToken")
            if not token:
                return None
    except Exception:  # noqa: BLE001
        return None


def invoke(client, runtime_arn: str, prompt: str, mode: str) -> tuple[str, str]:
    """Invoke the runtime; return (session_id, final_text)."""
    session_id = str(uuid.uuid4())
    payload = json.dumps({"prompt": prompt, "mode": mode}).encode("utf-8")

    resp = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        payload=payload,
        qualifier="DEFAULT",
    )

    chunks: list[str] = []
    body = resp.get("response")
    content_type = resp.get("contentType", "")
    if body is None:
        return session_id, ""

    raw = body.read().decode("utf-8", errors="replace")
    if "text/event-stream" in content_type:
        # SSE stream: extract text deltas from event lines
        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue
            delta = (
                event.get("event", {})
                .get("contentBlockDelta", {})
                .get("delta", {})
                .get("text")
            )
            if delta:
                chunks.append(delta)
        return session_id, "".join(chunks)
    return session_id, raw


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-arn", default=None,
                        help="AgentCore Runtime ARN (default: auto-discovered from the CDK stack)")
    parser.add_argument("--runtime", default=DEFAULT_RUNTIME,
                        help=f"Runtime name to discover and evaluate, e.g. langgraph_claims_agent "
                             f"(default: {DEFAULT_RUNTIME})")
    parser.add_argument("--stack-name", default=DEFAULT_STACK,
                        help=f"CloudFormation stack to discover the runtime from (default: {DEFAULT_STACK})")
    parser.add_argument("--mode", default="compliant", choices=["compliant", "lazy", "both"])
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--region", default=None)
    args = parser.parse_args()

    if args.runtime_arn:
        region = args.region or args.runtime_arn.split(":")[3]
        runtime_arn = args.runtime_arn
    else:
        region = (args.region or os.environ.get("AWS_REGION")
                  or os.environ.get("AWS_DEFAULT_REGION")
                  or boto3.Session().region_name)
        if not region:
            print("error: no region -- set AWS_REGION or pass --region/--runtime-arn", file=sys.stderr)
            return 1
        session = boto3.Session(region_name=region)
        runtime_arn = resolve_runtime_arn(session, args.stack_name, args.runtime)
        if not runtime_arn:
            print(f"error: could not discover runtime '{args.runtime}' from stack {args.stack_name} "
                  f"or the AgentCore control plane in {region}.", file=sys.stderr)
            print("Deploy first (quickstart step 1), or pass --runtime-arn explicitly.", file=sys.stderr)
            return 1
        print(f"runtime: {runtime_arn} (discovered)")

    client = boto3.client("bedrock-agentcore", region_name=region)

    modes = ["compliant", "lazy"] if args.mode == "both" else [args.mode]
    sessions: list[tuple[str, str]] = []
    for mode in modes:
        print(f"\n=== mode: {mode} ===")
        session_id, text = invoke(client, runtime_arn, args.prompt, mode)
        print(f"session: {session_id}")
        print(text.strip()[:1500])
        sessions.append((mode, session_id))

    print("\nNext: wait 2-5 minutes for spans to reach CloudWatch, then run from claimsdemo/:")
    for mode, session_id in sessions:
        print(f"\n  # {mode}")
        print(f"  agentcore run eval --runtime {args.runtime} \\")
        print(f"    --evaluator claims_contract --evaluator \"Builtin.Helpfulness\" \\")
        print(f"    --session-id {session_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
