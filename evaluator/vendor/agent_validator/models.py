# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Core data models for the agent behavioral validation toolkit."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class StepType(str, Enum):
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    RETRIEVAL = "retrieval"
    HUMAN_REVIEW = "human_review"
    CUSTOM = "custom"


class VerdictStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"


class ConstraintType(str, Enum):
    SOURCE = "source"
    STEP = "step"
    OUTPUT = "output"
    ESCALATION = "escalation"


class CheckMethod(str, Enum):
    DETERMINISTIC = "deterministic"
    HEURISTIC = "heuristic"
    LLM_JUDGE = "llm_judge"


@dataclass
class DataSourceRef:
    """A reference to an external data source consulted during execution."""

    source_name: str
    source_type: str  # "database", "api", "knowledge_base", "file"
    version: str | None = None
    query: str | None = None
    result_count: int | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ExecutionStep:
    """One discrete step in an agent execution."""

    step_id: str
    step_type: StepType
    name: str
    started_at: datetime
    ended_at: datetime | None = None
    parent_step_id: str | None = None
    status: str = "success"
    inputs: dict | None = None
    outputs: dict | None = None
    data_sources: list[DataSourceRef] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class NormalizedExecution:
    """A trace-source-agnostic record of a single agent execution."""

    execution_id: str
    trace_source: str
    started_at: datetime
    steps: list[ExecutionStep] = field(default_factory=list)
    ended_at: datetime | None = None
    input_text: str | None = None
    output_text: str | None = None
    metadata: dict = field(default_factory=dict)

    def tool_calls(self) -> list[ExecutionStep]:
        return [s for s in self.steps if s.step_type == StepType.TOOL_CALL]

    def model_calls(self) -> list[ExecutionStep]:
        return [s for s in self.steps if s.step_type == StepType.MODEL_CALL]

    def retrievals(self) -> list[ExecutionStep]:
        return [s for s in self.steps if s.step_type == StepType.RETRIEVAL]

    def step_names(self) -> list[str]:
        return [s.name for s in self.steps]

    def all_data_sources(self) -> list[DataSourceRef]:
        sources: list[DataSourceRef] = []
        for step in self.steps:
            sources.extend(step.data_sources)
        return sources

    def data_source_names(self) -> set[str]:
        return {ds.source_name for ds in self.all_data_sources()}


@dataclass
class Violation:
    """A single contract constraint violation."""

    constraint_name: str
    constraint_type: ConstraintType
    check_method: CheckMethod
    message: str
    severity: str = "error"
    evidence: dict = field(default_factory=dict)


@dataclass
class Verdict:
    """The result of validating an execution against a contract."""

    status: VerdictStatus
    contract_name: str
    execution_id: str
    violations: list[Violation] = field(default_factory=list)
    checks_performed: int = 0
    checks_passed: int = 0


@dataclass
class AuditRecord:
    """An audit-ready record combining verdict, execution summary, and evidence."""

    audit_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    verdict: Verdict | None = None
    contract_version: str = ""
    execution_summary: dict = field(default_factory=dict)
    evidence_package: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict."""
        return {
            "audit_id": self.audit_id,
            "timestamp": self.timestamp.isoformat(),
            "contract_version": self.contract_version,
            "verdict": {
                "status": self.verdict.status.value,
                "contract_name": self.verdict.contract_name,
                "execution_id": self.verdict.execution_id,
                "checks_performed": self.verdict.checks_performed,
                "checks_passed": self.verdict.checks_passed,
                "violations": [
                    {
                        "constraint_name": v.constraint_name,
                        "constraint_type": v.constraint_type.value,
                        "check_method": v.check_method.value,
                        "message": v.message,
                        "severity": v.severity,
                        "evidence": v.evidence,
                    }
                    for v in self.verdict.violations
                ],
            }
            if self.verdict
            else None,
            "execution_summary": self.execution_summary,
            "evidence_package": self.evidence_package,
        }
