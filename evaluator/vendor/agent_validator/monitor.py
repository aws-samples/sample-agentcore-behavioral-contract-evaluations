# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Production monitoring: continuously validate agent traces.

Watches a trace source (CloudWatch aws/spans, local file directory, or OTEL
collector export) and validates each complete trace against a behavioral contract.

Modes:
  - CloudWatch: polls aws/spans log group for new traces
  - Directory: watches a directory for new JSON trace files
  - Callable: accepts traces from your own pipeline
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent_validator.adapters.otel import OTELAdapter
from agent_validator.audit import create_audit_record
from agent_validator.contracts.base import Contract
from agent_validator.models import AuditRecord, VerdictStatus
from agent_validator.store import save_audit_record
from agent_validator.validator import validate

logger = logging.getLogger(__name__)


@dataclass
class MonitorStats:
    """Running statistics for the monitor."""

    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    traces_validated: int = 0
    passed: int = 0
    failed: int = 0
    warned: int = 0
    errors: int = 0

    @property
    def compliance_rate(self) -> float:
        if self.traces_validated == 0:
            return 0.0
        return self.passed / self.traces_validated

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "traces_validated": self.traces_validated,
            "passed": self.passed,
            "failed": self.failed,
            "warned": self.warned,
            "errors": self.errors,
            "compliance_rate": f"{self.compliance_rate:.1%}",
        }


OnViolation = Callable[[AuditRecord], None]


def _default_on_violation(record: AuditRecord) -> None:
    """Default violation handler: log to stderr."""
    v = record.verdict
    if v:
        violations = ", ".join(viol.constraint_name for viol in v.violations)
        logger.warning(
            "VIOLATION [%s] execution=%s checks=%d/%d violations: %s",
            v.contract_name, v.execution_id[:12], v.checks_passed, v.checks_performed, violations,
        )


def validate_trace(
    raw_trace: dict,
    contract: Contract,
    adapter: OTELAdapter,
    metadata: dict | None = None,
) -> AuditRecord:
    """Validate a single trace and return an audit record."""
    execution = adapter.normalize(raw_trace)
    if metadata:
        execution.metadata.update(metadata)
    verdict = validate(contract, execution)
    return create_audit_record(verdict, execution, contract.version)


def monitor_cloudwatch(
    contract: Contract,
    adapter: OTELAdapter,
    *,
    region: str = "us-west-2",
    log_group: str = "aws/spans",
    poll_interval: int = 30,
    output_dir: str | Path | None = None,
    metadata: dict | None = None,
    on_violation: OnViolation | None = None,
    max_iterations: int | None = None,
) -> MonitorStats:
    """Poll CloudWatch aws/spans for new traces and validate them.

    Args:
        contract: The behavioral contract to validate against.
        adapter: The OTEL adapter with tool_source_map configured.
        region: AWS region.
        log_group: CloudWatch log group name (default: aws/spans).
        poll_interval: Seconds between polls.
        output_dir: Directory to save audit records (optional).
        metadata: Extra metadata to set on each execution (e.g., claim_type).
        on_violation: Callback for violations. Defaults to logging.
        max_iterations: Stop after N poll cycles (None = run forever).
    """
    import boto3

    logs = boto3.client("logs", region_name=region)
    stats = MonitorStats()
    on_violation = on_violation or _default_on_violation
    seen_traces: set[str] = set()

    # Start from now
    last_timestamp = int(time.time() * 1000)

    iteration = 0
    logger.info("Monitor started: contract=%s log_group=%s region=%s poll=%ds",
                contract.name, log_group, region, poll_interval)

    try:
        while max_iterations is None or iteration < max_iterations:
            iteration += 1

            # Pull new events
            try:
                response = logs.filter_log_events(
                    logGroupName=log_group,
                    startTime=last_timestamp,
                    limit=100,
                )
            except Exception as e:
                logger.error("CloudWatch poll error: %s", e)
                stats.errors += 1
                time.sleep(poll_interval)
                continue

            # Parse spans
            spans: list[dict] = []
            max_ts = last_timestamp
            for event in response.get("events", []):
                if event.get("timestamp", 0) > max_ts:
                    max_ts = event["timestamp"]
                for entry in event["message"].split("\t"):
                    entry = entry.strip()
                    if not entry:
                        continue
                    try:
                        spans.append(json.loads(entry))
                    except (json.JSONDecodeError, ValueError):
                        pass

            if spans:
                # Group by traceId
                traces: dict[str, list[dict]] = defaultdict(list)
                for s in spans:
                    traces[s.get("traceId", "unknown")].append(s)

                # Validate complete traces (those with tool calls)
                for trace_id, trace_spans in traces.items():
                    if trace_id in seen_traces:
                        continue

                    tool_count = sum(1 for s in trace_spans if "execute_tool" in s.get("name", ""))
                    if tool_count == 0:
                        continue  # Skip incomplete traces

                    seen_traces.add(trace_id)

                    # Convert to OTLP format
                    otlp = _cloudwatch_spans_to_otlp(trace_spans)

                    try:
                        record = validate_trace(otlp, contract, adapter, metadata)
                        stats.traces_validated += 1

                        if record.verdict:
                            status = record.verdict.status
                            if status == VerdictStatus.PASS:
                                stats.passed += 1
                            elif status == VerdictStatus.FAIL:
                                stats.failed += 1
                                on_violation(record)
                            elif status == VerdictStatus.WARN:
                                stats.warned += 1

                        if output_dir:
                            save_audit_record(record, output_dir)

                        logger.info(
                            "Validated trace=%s status=%s checks=%d/%d",
                            trace_id[:12],
                            record.verdict.status.value if record.verdict else "?",
                            record.verdict.checks_passed if record.verdict else 0,
                            record.verdict.checks_performed if record.verdict else 0,
                        )
                    except Exception as e:
                        logger.error("Validation error for trace %s: %s", trace_id[:12], e)
                        stats.errors += 1

                last_timestamp = max_ts + 1  # Move past the last seen event

            if max_iterations is not None and iteration >= max_iterations:
                break

            time.sleep(poll_interval)

    except KeyboardInterrupt:
        logger.info("Monitor stopped by user")

    logger.info("Monitor stats: %s", stats.to_dict())
    return stats


def monitor_directory(
    contract: Contract,
    adapter: OTELAdapter,
    *,
    watch_dir: str | Path,
    poll_interval: int = 5,
    output_dir: str | Path | None = None,
    metadata: dict | None = None,
    on_violation: OnViolation | None = None,
    max_iterations: int | None = None,
) -> MonitorStats:
    """Watch a directory for new trace JSON files and validate them."""
    watch_dir = Path(watch_dir)
    stats = MonitorStats()
    on_violation = on_violation or _default_on_violation
    seen_files: set[str] = set()

    # Mark existing files as seen
    for f in watch_dir.glob("*.json"):
        seen_files.add(str(f))

    iteration = 0
    logger.info("Monitor started: watching %s", watch_dir)

    try:
        while max_iterations is None or iteration < max_iterations:
            iteration += 1

            for f in sorted(watch_dir.glob("*.json")):
                if str(f) in seen_files:
                    continue
                seen_files.add(str(f))

                try:
                    with open(f) as fh:
                        raw_trace = json.load(fh)
                    record = validate_trace(raw_trace, contract, adapter, metadata)
                    stats.traces_validated += 1

                    if record.verdict:
                        status = record.verdict.status
                        if status == VerdictStatus.PASS:
                            stats.passed += 1
                        elif status == VerdictStatus.FAIL:
                            stats.failed += 1
                            on_violation(record)
                        elif status == VerdictStatus.WARN:
                            stats.warned += 1

                    if output_dir:
                        save_audit_record(record, output_dir)

                    logger.info("Validated %s: %s", f.name, record.verdict.status.value if record.verdict else "?")
                except Exception as e:
                    logger.error("Error validating %s: %s", f.name, e)
                    stats.errors += 1

            if max_iterations is not None and iteration >= max_iterations:
                break

            time.sleep(poll_interval)

    except KeyboardInterrupt:
        logger.info("Monitor stopped by user")

    logger.info("Monitor stats: %s", stats.to_dict())
    return stats


def _cloudwatch_spans_to_otlp(spans: list[dict]) -> dict:
    """Convert CloudWatch span dicts to OTLP JSON format."""
    otlp_spans = []
    for s in spans:
        attrs = s.get("attributes", {})
        if isinstance(attrs, dict):
            otlp_attrs = []
            for k, v in attrs.items():
                if isinstance(v, bool):
                    otlp_attrs.append({"key": k, "value": {"boolValue": v}})
                elif isinstance(v, int):
                    otlp_attrs.append({"key": k, "value": {"intValue": v}})
                elif isinstance(v, float):
                    otlp_attrs.append({"key": k, "value": {"doubleValue": v}})
                else:
                    otlp_attrs.append({"key": k, "value": {"stringValue": str(v)}})
        else:
            otlp_attrs = attrs

        events = []
        for e in s.get("events", []):
            ea = e.get("attributes", {})
            if isinstance(ea, dict):
                ea = [{"key": k, "value": {"stringValue": str(v)}} for k, v in ea.items()]
            events.append({"name": e.get("name", ""), "timeUnixNano": str(e.get("timeUnixNano", 0)), "attributes": ea})

        otlp_spans.append({
            "traceId": s.get("traceId", ""),
            "spanId": s.get("spanId", ""),
            "parentSpanId": s.get("parentSpanId", ""),
            "name": s.get("name", ""),
            "startTimeUnixNano": str(s.get("startTimeUnixNano", 0)),
            "endTimeUnixNano": str(s.get("endTimeUnixNano", 0)),
            "kind": s.get("kind", 0),
            "status": s.get("status", {"code": 0}),
            "attributes": otlp_attrs,
            "events": events,
        })

    return {
        "resourceSpans": [{
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "monitored-agent"}}]},
            "scopeSpans": [{"scope": {"name": "agent"}, "spans": otlp_spans}],
        }]
    }
