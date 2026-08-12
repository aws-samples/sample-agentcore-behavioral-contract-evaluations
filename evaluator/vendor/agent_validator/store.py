# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Audit record persistence.

Saves audit records to local files or S3 for compliance storage.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agent_validator.models import AuditRecord


def save_audit_record(record: AuditRecord, output_dir: str | Path) -> Path:
    """Save a single audit record as a JSON file.

    Filename: {verdict}_{execution_id}_{timestamp}.json
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    status = record.verdict.status.value if record.verdict else "unknown"
    exec_id = record.verdict.execution_id[:12] if record.verdict else "unknown"
    ts = record.timestamp.strftime("%Y%m%dT%H%M%S")
    filename = f"{status}_{exec_id}_{ts}.json"

    path = output_dir / filename
    with open(path, "w") as f:
        json.dump(record.to_dict(), f, indent=2, default=str)
    return path


def save_batch_report(
    records: list[AuditRecord],
    contract_name: str,
    contract_version: str,
    output_dir: str | Path,
) -> Path:
    """Save a batch validation report with summary + all audit records."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    counts = {"pass": 0, "fail": 0, "warn": 0}
    for r in records:
        if r.verdict:
            counts[r.verdict.status.value] += 1

    report = {
        "contract": contract_name,
        "contract_version": contract_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total": len(records),
        "passed": counts["pass"],
        "failed": counts["fail"],
        "warnings": counts["warn"],
        "records": [r.to_dict() for r in records],
    }

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = output_dir / f"report_{contract_name}_{ts}.json"
    with open(path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    return path


def save_to_s3(record: AuditRecord, bucket: str, prefix: str = "audit-records") -> str:
    """Save an audit record to S3. Returns the S3 key."""
    import boto3

    s3 = boto3.client("s3")
    status = record.verdict.status.value if record.verdict else "unknown"
    exec_id = record.verdict.execution_id[:12] if record.verdict else "unknown"
    ts = record.timestamp.strftime("%Y/%m/%d")
    key = f"{prefix}/{ts}/{status}_{exec_id}_{record.audit_id}.json"

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(record.to_dict(), indent=2, default=str),
        ContentType="application/json",
    )
    return f"s3://{bucket}/{key}"
