# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Audit record generation and rendering."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from agent_validator.models import AuditRecord, NormalizedExecution, Verdict


def create_audit_record(
    verdict: Verdict,
    execution: NormalizedExecution,
    contract_version: str,
) -> AuditRecord:
    """Create an audit-ready record from a verdict and execution."""
    evidence = []
    for violation in verdict.violations:
        evidence.append(
            {
                "constraint": violation.constraint_name,
                "type": violation.constraint_type.value,
                "method": violation.check_method.value,
                "message": violation.message,
                "severity": violation.severity,
                "evidence": violation.evidence,
            }
        )

    return AuditRecord(
        audit_id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        verdict=verdict,
        contract_version=contract_version,
        execution_summary={
            "execution_id": execution.execution_id,
            "trace_source": execution.trace_source,
            "started_at": execution.started_at.isoformat(),
            "ended_at": execution.ended_at.isoformat() if execution.ended_at else None,
            "step_count": len(execution.steps),
            "tool_calls": [s.name for s in execution.tool_calls()],
            "data_sources_consulted": sorted(execution.data_source_names()),
            "input_preview": (execution.input_text or "")[:200] or None,
            "output_preview": (execution.output_text or "")[:200] or None,
        },
        evidence_package=evidence,
    )


def render_audit_report(record: AuditRecord) -> str:
    """Render a human-readable audit report for terminal output."""
    v = record.verdict
    lines = [
        f"{'=' * 70}",
        f"  AUDIT RECORD: {record.audit_id}",
        f"  Timestamp:    {record.timestamp.isoformat()}",
        f"  Contract:     {v.contract_name} v{record.contract_version}" if v else "",
        f"  Verdict:      {v.status.value.upper()}" if v else "",
        f"  Checks:       {v.checks_passed}/{v.checks_performed} passed" if v else "",
        f"{'=' * 70}",
    ]

    if v and v.violations:
        lines.append("")
        lines.append(f"  VIOLATIONS ({len(v.violations)}):")
        lines.append(f"  {'-' * 66}")
        for violation in v.violations:
            severity_tag = violation.severity.upper()
            lines.append(f"  [{severity_tag}] {violation.constraint_name}")
            lines.append(f"         {violation.message}")
            lines.append(f"         method: {violation.check_method.value}")
            if violation.evidence:
                for key, val in violation.evidence.items():
                    lines.append(f"         {key}: {val}")
            lines.append("")

    summary = record.execution_summary
    if summary:
        lines.append(f"  EXECUTION SUMMARY:")
        lines.append(f"  {'-' * 66}")
        lines.append(f"  ID:           {summary.get('execution_id', 'N/A')}")
        lines.append(f"  Source:       {summary.get('trace_source', 'N/A')}")
        lines.append(f"  Steps:        {summary.get('step_count', 0)}")
        lines.append(f"  Tool calls:   {summary.get('tool_calls', [])}")
        lines.append(f"  Data sources: {summary.get('data_sources_consulted', [])}")

    lines.append(f"{'=' * 70}")
    return "\n".join(lines)
