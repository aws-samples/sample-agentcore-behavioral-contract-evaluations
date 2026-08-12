# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Concrete constraint implementations and builder functions."""

from __future__ import annotations

import re
from collections.abc import Callable

from agent_validator.models import (
    CheckMethod,
    ConstraintType,
    NormalizedExecution,
    StepType,
    Violation,
)
from agent_validator.contracts.base import Constraint, ConstraintResult


# ---------------------------------------------------------------------------
# Source constraints (deterministic)
# ---------------------------------------------------------------------------


class MustRetrieveFrom(Constraint):
    """At least one step must consult each of the required data sources."""

    constraint_type = ConstraintType.SOURCE
    check_method = CheckMethod.DETERMINISTIC

    def __init__(self, required_sources: list[str], *, severity: str = "error"):
        self.required_sources = required_sources
        self.severity = severity
        self.name = f"must_retrieve_from({required_sources})"

    def evaluate(self, execution: NormalizedExecution) -> ConstraintResult:
        consulted = execution.data_source_names()
        missing = [s for s in self.required_sources if s not in consulted]
        if missing:
            return ConstraintResult(
                passed=False,
                violation=Violation(
                    constraint_name=self.name,
                    constraint_type=self.constraint_type,
                    check_method=self.check_method,
                    message=f"Required data sources not consulted: {missing}",
                    severity=self.severity,
                    evidence={
                        "required": self.required_sources,
                        "consulted": sorted(consulted),
                        "missing": missing,
                    },
                ),
            )
        return ConstraintResult(passed=True)


class MustNotUseOnlyParametricKnowledge(Constraint):
    """Execution must include at least one tool call or retrieval step."""

    constraint_type = ConstraintType.SOURCE
    check_method = CheckMethod.DETERMINISTIC
    name = "must_not_use_only_parametric_knowledge"

    def __init__(self, *, severity: str = "error"):
        self.severity = severity

    def evaluate(self, execution: NormalizedExecution) -> ConstraintResult:
        has_external = any(
            s.step_type in (StepType.TOOL_CALL, StepType.RETRIEVAL) for s in execution.steps
        )
        if not has_external:
            return ConstraintResult(
                passed=False,
                violation=Violation(
                    constraint_name=self.name,
                    constraint_type=self.constraint_type,
                    check_method=self.check_method,
                    message="Agent answered using only parametric knowledge (no tool calls or retrievals)",
                    severity=self.severity,
                    evidence={"step_types": [s.step_type.value for s in execution.steps]},
                ),
            )
        return ConstraintResult(passed=True)


# ---------------------------------------------------------------------------
# Step constraints (deterministic)
# ---------------------------------------------------------------------------


class MustIncludeSteps(Constraint):
    """All named steps must appear in the execution."""

    constraint_type = ConstraintType.STEP
    check_method = CheckMethod.DETERMINISTIC

    def __init__(self, required_steps: list[str], *, severity: str = "error"):
        self.required_steps = required_steps
        self.severity = severity
        self.name = f"must_include_steps({required_steps})"

    def evaluate(self, execution: NormalizedExecution) -> ConstraintResult:
        present = set(execution.step_names())
        missing = [s for s in self.required_steps if s not in present]
        if missing:
            return ConstraintResult(
                passed=False,
                violation=Violation(
                    constraint_name=self.name,
                    constraint_type=self.constraint_type,
                    check_method=self.check_method,
                    message=f"Required steps missing from execution: {missing}",
                    severity=self.severity,
                    evidence={
                        "required": self.required_steps,
                        "present": sorted(present),
                        "missing": missing,
                    },
                ),
            )
        return ConstraintResult(passed=True)


class MustNotIncludeSteps(Constraint):
    """None of the named steps may appear in the execution.

    The negative counterpart of MustIncludeSteps. Useful for branch-specific
    prohibitions, e.g. "on the escalation path, the agent must NOT render a
    decision" -- typically wrapped in a ConditionalConstraint.
    """

    constraint_type = ConstraintType.STEP
    check_method = CheckMethod.DETERMINISTIC

    def __init__(self, forbidden_steps: list[str], *, severity: str = "error"):
        self.forbidden_steps = forbidden_steps
        self.severity = severity
        self.name = f"must_not_include_steps({forbidden_steps})"

    def evaluate(self, execution: NormalizedExecution) -> ConstraintResult:
        present = set(execution.step_names())
        found = [s for s in self.forbidden_steps if s in present]
        if found:
            return ConstraintResult(
                passed=False,
                violation=Violation(
                    constraint_name=self.name,
                    constraint_type=self.constraint_type,
                    check_method=self.check_method,
                    message=f"Forbidden steps present in execution: {found}",
                    severity=self.severity,
                    evidence={
                        "forbidden": self.forbidden_steps,
                        "found": found,
                        "steps": sorted(present),
                    },
                ),
            )
        return ConstraintResult(passed=True)


class MustPrecede(Constraint):
    """Step A must appear before step B in the execution."""

    constraint_type = ConstraintType.STEP
    check_method = CheckMethod.DETERMINISTIC

    def __init__(self, before: str, after: str, *, severity: str = "error"):
        self.before = before
        self.after = after
        self.severity = severity
        self.name = f"must_precede({before}, {after})"

    def evaluate(self, execution: NormalizedExecution) -> ConstraintResult:
        names = execution.step_names()

        if self.before not in names:
            return ConstraintResult(
                passed=False,
                violation=Violation(
                    constraint_name=self.name,
                    constraint_type=self.constraint_type,
                    check_method=self.check_method,
                    message=f"Preceding step '{self.before}' not found in execution",
                    severity=self.severity,
                    evidence={"expected_before": self.before, "steps": names},
                ),
            )

        if self.after not in names:
            # The "after" step is missing — ordering can't be violated, but the step
            # itself may be caught by MustIncludeSteps. Pass here to avoid double-reporting.
            return ConstraintResult(passed=True)

        first_before = names.index(self.before)
        first_after = names.index(self.after)

        if first_before >= first_after:
            return ConstraintResult(
                passed=False,
                violation=Violation(
                    constraint_name=self.name,
                    constraint_type=self.constraint_type,
                    check_method=self.check_method,
                    message=(
                        f"Step '{self.before}' (position {first_before}) did not precede "
                        f"'{self.after}' (position {first_after})"
                    ),
                    severity=self.severity,
                    evidence={
                        "expected_before": self.before,
                        "expected_after": self.after,
                        "actual_order": names,
                    },
                ),
            )
        return ConstraintResult(passed=True)


# ---------------------------------------------------------------------------
# Output constraints (heuristic for MVP)
# ---------------------------------------------------------------------------

# Common citation patterns: "Section 4.2.1", "Policy XYZ", "[1]", "(Ref: ...)"
_CITATION_PATTERNS = [
    r"[Ss]ection\s+[\d.]+",
    r"[Pp]olicy\s+[\w.\-]+",
    r"\[\d+\]",
    r"\([Rr]ef:?\s*[^)]+\)",
    r"[Aa]rticle\s+\d+",
    r"\d+\s+CFR\s+[\d.]+",                  # US Code of Federal Regulations (31 CFR 1010.230)
    r"[Dd]irective\s+\(?EU\)?\s*[\d/]+",    # EU Directives (Directive (EU) 2024/1640)
    r"[Rr]eg(?:ulation)?\.?\s+\d+",         # UK/generic regulations (Reg. 33, Regulation 5)
    r"[Aa]rt\.?\s+\d+",                     # Article abbreviation (Art. 28)
]


class OutputMustContainCitations(Constraint):
    """Final output must contain at least min_count citation references."""

    constraint_type = ConstraintType.OUTPUT
    check_method = CheckMethod.HEURISTIC

    def __init__(self, min_count: int = 1, *, severity: str = "error"):
        self.min_count = min_count
        self.severity = severity
        self.name = f"output_must_contain_citations(min={min_count})"

    def evaluate(self, execution: NormalizedExecution) -> ConstraintResult:
        output = execution.output_text or ""
        if not output:
            return ConstraintResult(
                passed=False,
                violation=Violation(
                    constraint_name=self.name,
                    constraint_type=self.constraint_type,
                    check_method=self.check_method,
                    message="No output text available to check for citations",
                    severity=self.severity,
                    evidence={"output_text": None},
                ),
            )

        found_citations: list[str] = []
        for pattern in _CITATION_PATTERNS:
            found_citations.extend(re.findall(pattern, output))

        if len(found_citations) < self.min_count:
            return ConstraintResult(
                passed=False,
                violation=Violation(
                    constraint_name=self.name,
                    constraint_type=self.constraint_type,
                    check_method=self.check_method,
                    message=(
                        f"Output contains {len(found_citations)} citation(s), "
                        f"minimum required is {self.min_count}"
                    ),
                    severity=self.severity,
                    evidence={
                        "found_citations": found_citations,
                        "min_required": self.min_count,
                        "output_preview": output[:200],
                    },
                ),
            )
        return ConstraintResult(passed=True)


# ---------------------------------------------------------------------------
# Escalation constraints (deterministic)
# ---------------------------------------------------------------------------

DEFAULT_ESCALATION_MARKERS = ["flag_for_human_review", "escalate", "human_review"]


class MustEscalateWhen(Constraint):
    """When a condition is detected, execution must include an escalation step."""

    constraint_type = ConstraintType.ESCALATION
    check_method = CheckMethod.DETERMINISTIC

    def __init__(
        self,
        condition_fn: Callable[[NormalizedExecution], bool],
        description: str,
        *,
        escalation_markers: list[str] | None = None,
        severity: str = "error",
    ):
        self.condition_fn = condition_fn
        self.description = description
        self.escalation_markers = escalation_markers or DEFAULT_ESCALATION_MARKERS
        self.severity = severity
        self.name = f"must_escalate_when({description})"

    def evaluate(self, execution: NormalizedExecution) -> ConstraintResult:
        if not self.condition_fn(execution):
            # Condition not triggered — nothing to check
            return ConstraintResult(passed=True)

        has_escalation = any(
            s.name in self.escalation_markers or s.step_type == StepType.HUMAN_REVIEW
            for s in execution.steps
        )

        if not has_escalation:
            return ConstraintResult(
                passed=False,
                violation=Violation(
                    constraint_name=self.name,
                    constraint_type=self.constraint_type,
                    check_method=self.check_method,
                    message=(
                        f"Escalation condition triggered ({self.description}) "
                        f"but no escalation step found"
                    ),
                    severity=self.severity,
                    evidence={
                        "condition": self.description,
                        "escalation_markers_checked": self.escalation_markers,
                        "steps_found": execution.step_names(),
                    },
                ),
            )
        return ConstraintResult(passed=True)


# ---------------------------------------------------------------------------
# Conditional constraints (dynamic contracts)
# ---------------------------------------------------------------------------


class ConditionalConstraint(Constraint):
    """A constraint that only evaluates when a condition on execution metadata is met.

    This enables dynamic contracts — one contract that adapts its constraints
    based on context (claim type, jurisdiction, risk level, etc.).
    """

    def __init__(
        self,
        condition: Callable[[NormalizedExecution], bool] | dict[str, str | int | float | bool],
        inner: Constraint,
        description: str = "",
    ):
        """Create a conditional constraint.

        Args:
            condition: Either a callable that takes NormalizedExecution and returns bool,
                or a dict of metadata key-value pairs that must all match.
            inner: The constraint to evaluate when the condition is met.
            description: Human-readable description of the condition.
        """
        if isinstance(condition, dict):
            match_dict = condition
            self.condition_fn = lambda ex: all(
                ex.metadata.get(k) == v for k, v in match_dict.items()
            )
            self.description = description or " AND ".join(f"{k}={v}" for k, v in match_dict.items())
        else:
            self.condition_fn = condition
            self.description = description or "custom condition"

        self.inner = inner
        self.name = f"when({self.description}): {inner.name}"
        self.constraint_type = inner.constraint_type
        self.check_method = inner.check_method
        self.severity = inner.severity

    def evaluate(self, execution: NormalizedExecution) -> ConstraintResult:
        if not self.condition_fn(execution):
            # Condition not met — skip this constraint (counts as pass)
            return ConstraintResult(passed=True)
        return self.inner.evaluate(execution)


# ---------------------------------------------------------------------------
# Builder functions (public API)
# ---------------------------------------------------------------------------


def must_retrieve_from(sources: list[str], *, severity: str = "error") -> MustRetrieveFrom:
    return MustRetrieveFrom(sources, severity=severity)


def must_not_use_only_parametric_knowledge(
    *, severity: str = "error",
) -> MustNotUseOnlyParametricKnowledge:
    return MustNotUseOnlyParametricKnowledge(severity=severity)


def must_include_steps(steps: list[str], *, severity: str = "error") -> MustIncludeSteps:
    return MustIncludeSteps(steps, severity=severity)


def must_not_include_steps(steps: list[str], *, severity: str = "error") -> MustNotIncludeSteps:
    return MustNotIncludeSteps(steps, severity=severity)


def must_precede(before: str, after: str, *, severity: str = "error") -> MustPrecede:
    return MustPrecede(before, after, severity=severity)


def must_contain_citations(min_count: int = 1, *, severity: str = "error") -> OutputMustContainCitations:
    return OutputMustContainCitations(min_count, severity=severity)


def must_escalate_when(
    condition_fn: Callable[[NormalizedExecution], bool],
    description: str,
    *,
    escalation_markers: list[str] | None = None,
    severity: str = "error",
) -> MustEscalateWhen:
    return MustEscalateWhen(
        condition_fn, description, escalation_markers=escalation_markers, severity=severity
    )


def when(
    condition: Callable[[NormalizedExecution], bool] | dict[str, str | int | float | bool],
    constraint: Constraint,
    description: str = "",
) -> ConditionalConstraint:
    """Make a constraint conditional — it only evaluates when the condition is met.

    Args:
        condition: Either a dict of metadata key=value pairs (all must match),
            or a callable that takes NormalizedExecution and returns bool.
        constraint: The constraint to evaluate when the condition is met.
        description: Human-readable description of the condition.

    Examples:
        # Only check formulary for pharmacy claims
        when({"claim_type": "pharmacy"}, must_include_steps(["lookup_formulary"]))

        # Only check jurisdiction for EU customers
        when({"jurisdiction": "EU"}, must_include_steps(["verify_eu_regulations"]))

        # Custom condition
        when(lambda ex: ex.metadata.get("risk_score", 0) > 50, must_include_steps(["enhanced_review"]))
    """
    return ConditionalConstraint(condition, constraint, description)
