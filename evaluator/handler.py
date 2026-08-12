"""AgentCore Evaluations code-based evaluator: behavioral contract validation.

Receives the code-based evaluator payload from AgentCore Evaluations, routes
the target trace to the behavioral contract(s) declared for the agent that
produced it, validates, and returns a verdict in the evaluator response
schema:

    {"label": "PASS" | "FAIL" | "WARN", "value": 0.0-1.0, "explanation": "..."}

Contract routing (one evaluator, many agents)
---------------------------------------------
All YAML files in CONTRACTS_DIR are loaded at cold start. Each contract may
declare which agents it applies to via glob patterns matched against the
service name AgentCore stamps on every span (e.g.
"claimsdemo_claims_agent.DEFAULT"):

    applies_to:
      - "*claims_agent*"

Routing rules:
- Every contract whose pattern matches the trace's service name is evaluated;
  multiple matches are aggregated (worst label wins).
- A contract without `applies_to` is a catch-all ONLY when it is the sole
  contract deployed (the simple single-agent case needs no routing config).
- If no contract matches, the evaluator returns an UNKNOWN_AGENT error rather
  than a misleading FAIL -- a mis-wired evaluator should fail loudly.

The contract engine is agentic-behavioral-contracts, used unchanged. This
handler is glue: parse payload -> route -> shim spans -> normalize ->
validate -> map.

Environment variables:
    CONTRACTS_DIR  Directory of contract YAML files (default: contracts/)
    AUDIT_BUCKET   Optional S3 bucket for full audit records. Disabled if unset.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

# Vendored libraries (agent_validator, agentic_otel) live in vendor/ for local
# development and tests. The Lambda build copies them to the package root, so
# this shim is a no-op at runtime.
_VENDOR = Path(__file__).parent / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

import yaml

from agent_validator.adapters.otel import OTELAdapter
from agent_validator.audit import create_audit_record
from agent_validator.contracts.base import Contract
from agent_validator.generate import load_contract_from_yaml
from agent_validator.models import NormalizedExecution, Verdict, VerdictStatus
from agent_validator.validator import validate

from span_shim import extract_service_names, shim_to_otlp

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CONTRACTS_DIR = os.environ.get("CONTRACTS_DIR", str(Path(__file__).parent / "contracts"))
AUDIT_BUCKET = os.environ.get("AUDIT_BUCKET", "")
# When truthy (and AUDIT_BUCKET is set), each raw evaluator payload is also
# written to s3://AUDIT_BUCKET/debug/ -- useful for capturing real payloads
# as test fixtures when adapting the evaluator to your own agent.
DEBUG_CAPTURE = os.environ.get("DEBUG_CAPTURE", "").lower() in ("1", "true", "yes")

_LABEL_RANK = {VerdictStatus.PASS: 0, VerdictStatus.WARN: 1, VerdictStatus.FAIL: 2}


@dataclass
class ContractEntry:
    """A loaded contract with its routing patterns and adapter."""

    contract: Contract
    adapter: OTELAdapter
    applies_to: list[str]
    path: str


def load_registry(contracts_dir: str | Path) -> list[ContractEntry]:
    """Load every contract YAML in the directory. Fails fast on invalid YAML."""
    entries: list[ContractEntry] = []
    paths = sorted(Path(contracts_dir).glob("*.yaml")) + sorted(Path(contracts_dir).glob("*.yml"))
    for path in paths:
        contract, source_map = load_contract_from_yaml(path)
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        applies_to = [str(p) for p in raw.get("applies_to") or []]
        entries.append(
            ContractEntry(
                contract=contract,
                adapter=OTELAdapter(tool_source_map=source_map),
                applies_to=applies_to,
                path=str(path),
            )
        )
        logger.info(
            "Loaded contract %s v%s (applies_to=%s) from %s",
            contract.name, contract.version, applies_to or "catch-all", path.name,
        )
    if not entries:
        raise RuntimeError(f"No contract YAML files found in {contracts_dir}")
    if len(entries) > 1:
        for e in entries:
            if not e.applies_to:
                logger.warning(
                    "Contract %s has no applies_to and will never be routed "
                    "(catch-all only applies when a single contract is deployed)",
                    e.contract.name,
                )
    return entries


def route_contracts(entries: list[ContractEntry], service_names: set[str]) -> list[ContractEntry]:
    """Select the contracts that apply to the given service identities."""
    matched = [
        e for e in entries
        if e.applies_to and any(
            fnmatch(svc.lower(), pattern.lower())
            for svc in service_names
            for pattern in e.applies_to
        )
    ]
    if matched:
        return matched
    # Single deployed contract without routing config: the simple case.
    if len(entries) == 1 and not entries[0].applies_to:
        return entries
    return []


REGISTRY = load_registry(CONTRACTS_DIR)

_s3 = None


def _put_audit_record(verdict: Verdict, execution: NormalizedExecution, contract: Contract) -> str | None:
    """Write the full audit record to S3. Never fails the evaluation."""
    global _s3
    try:
        import boto3

        if _s3 is None:
            _s3 = boto3.client("s3")
        record = create_audit_record(verdict, execution, contract.version)
        key = f"audit/{verdict.status.value.upper()}/{execution.execution_id}_{contract.name}.json"
        _s3.put_object(
            Bucket=AUDIT_BUCKET,
            Key=key,
            Body=json.dumps(record.to_dict(), indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        return record.audit_id
    except Exception:  # noqa: BLE001 - audit side-channel must not fail evaluation
        logger.exception("Failed to write audit record to s3://%s", AUDIT_BUCKET)
        return None


def _put_debug_capture(event: dict) -> None:
    """Write the raw evaluator payload to S3 for fixture capture. Best-effort."""
    global _s3
    try:
        import uuid

        import boto3

        if _s3 is None:
            _s3 = boto3.client("s3")
        name = event.get("evaluatorName", "unknown")
        _s3.put_object(
            Bucket=AUDIT_BUCKET,
            Key=f"debug/{name}_{uuid.uuid4().hex[:8]}.json",
            Body=json.dumps(event, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception:  # noqa: BLE001 - debug side-channel must not fail evaluation
        logger.exception("Failed to write debug capture to s3://%s", AUDIT_BUCKET)


def _format_segment(verdict: Verdict, audit_id: str | None) -> str:
    lines = [
        f"Contract {verdict.contract_name}: {verdict.checks_passed}/{verdict.checks_performed} checks passed."
    ]
    for v in verdict.violations:
        lines.append(f"[x] {v.constraint_name}: {v.message}")
    if not verdict.violations:
        lines.append("All behavioral constraints satisfied.")
    if audit_id:
        lines.append(f"audit_id={audit_id}")
    return " ".join(lines)


def handler(event, context):
    logger.info(
        "Evaluator %s level=%s target=%s",
        event.get("evaluatorName"),
        event.get("evaluationLevel"),
        event.get("evaluationTarget"),
    )

    if DEBUG_CAPTURE and AUDIT_BUCKET:
        _put_debug_capture(event)

    session_spans = (event.get("evaluationInput") or {}).get("sessionSpans") or []
    if not session_spans:
        return {"errorCode": "NO_SPANS", "errorMessage": "evaluationInput.sessionSpans is empty"}

    target = event.get("evaluationTarget") or {}
    target_trace_ids = set(target.get("traceIds") or [])

    otlp = shim_to_otlp(session_spans, target_trace_ids or None)
    if not otlp["spans"]:
        return {
            "errorCode": "NO_TARGET_SPANS",
            "errorMessage": f"No spans matched target traces {sorted(target_trace_ids)}",
        }

    service_names = extract_service_names(otlp["spans"])
    matched = route_contracts(REGISTRY, service_names)
    if not matched:
        known = {e.contract.name: e.applies_to for e in REGISTRY}
        return {
            "errorCode": "UNKNOWN_AGENT",
            "errorMessage": (
                f"No contract applies to service(s) {sorted(service_names)}. "
                f"Known contracts: {json.dumps(known)}. Add an applies_to pattern "
                f"for this agent or remove the evaluator from its eval config."
            ),
        }

    verdicts: list[Verdict] = []
    segments: list[str] = []
    try:
        for entry in matched:
            execution = entry.adapter.normalize(otlp)
            verdict = validate(entry.contract, execution)
            audit_id = _put_audit_record(verdict, execution, entry.contract) if AUDIT_BUCKET else None
            verdicts.append(verdict)
            segments.append(_format_segment(verdict, audit_id))
    except Exception as exc:  # noqa: BLE001 - surface as evaluator error, not Lambda crash
        logger.exception("Contract evaluation failed")
        return {"errorCode": "EVALUATOR_ERROR", "errorMessage": str(exc)[:512]}

    worst = max(verdicts, key=lambda v: _LABEL_RANK[v.status])
    total_performed = sum(v.checks_performed for v in verdicts)
    total_passed = sum(v.checks_passed for v in verdicts)

    result = {
        "label": worst.status.value.upper(),
        "value": total_passed / max(total_performed, 1),
        "explanation": " | ".join(segments),
    }
    logger.info("Verdict: %s (%d contract(s): %s)", result["label"], len(matched),
                ", ".join(v.contract_name for v in verdicts))
    return result
