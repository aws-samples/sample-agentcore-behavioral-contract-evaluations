# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Discovery and dry-run utilities for building contracts.

Helps users inspect what their agent traces contain and verify that
contracts will match against real executions.
"""

from __future__ import annotations

from agent_validator.contracts.base import Contract
from agent_validator.models import NormalizedExecution, StepType


def describe_execution(execution: NormalizedExecution) -> str:
    """Summarize what an execution contains — use this to understand your traces
    before writing contracts.

    Returns a human-readable summary of step names, data sources, step types,
    and output characteristics that you can use to inform contract definitions.
    """
    lines = [
        "=" * 60,
        "  EXECUTION DISCOVERY",
        f"  ID: {execution.execution_id}",
        f"  Source: {execution.trace_source}",
        f"  Steps: {len(execution.steps)}",
        "=" * 60,
        "",
    ]

    # Step names by type
    tool_calls = execution.tool_calls()
    model_calls = execution.model_calls()
    retrievals = execution.retrievals()
    other = [
        s for s in execution.steps
        if s.step_type not in (StepType.TOOL_CALL, StepType.MODEL_CALL, StepType.RETRIEVAL)
    ]

    if tool_calls:
        lines.append("  TOOL CALLS (use these names in must_include_steps / must_precede):")
        lines.append("  " + "-" * 56)
        for s in tool_calls:
            sources = [ds.source_name for ds in s.data_sources]
            source_str = f"  -> data sources: {sources}" if sources else ""
            lines.append(f"    {s.name}{source_str}")
        lines.append("")

    if model_calls:
        lines.append("  MODEL CALLS:")
        lines.append("  " + "-" * 56)
        for s in model_calls:
            model = s.metadata.get("model", "unknown")
            lines.append(f"    {s.name} (model: {model})")
        lines.append("")

    if retrievals:
        lines.append("  RETRIEVALS:")
        lines.append("  " + "-" * 56)
        for s in retrievals:
            sources = [ds.source_name for ds in s.data_sources]
            lines.append(f"    {s.name} -> {sources}")
        lines.append("")

    if other:
        lines.append("  OTHER STEPS:")
        lines.append("  " + "-" * 56)
        for s in other:
            lines.append(f"    {s.name} (type: {s.step_type.value})")
        lines.append("")

    # Data sources
    all_sources = execution.data_source_names()
    lines.append("  DATA SOURCES CONSULTED (use these in must_retrieve_from):")
    lines.append("  " + "-" * 56)
    if all_sources:
        for name in sorted(all_sources):
            lines.append(f"    {name}")
    else:
        lines.append("    (none detected — check your adapter's tool_source_map)")
    lines.append("")

    # Step order
    lines.append("  STEP ORDER (use this to define must_precede constraints):")
    lines.append("  " + "-" * 56)
    for i, s in enumerate(execution.steps):
        lines.append(f"    [{i}] {s.name} ({s.step_type.value})")
    lines.append("")

    # Output
    lines.append("  OUTPUT (check for citations, grounding):")
    lines.append("  " + "-" * 56)
    output = execution.output_text or "(no output text captured)"
    lines.append(f"    {output[:300]}")
    lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def dry_run(contract: Contract, execution: NormalizedExecution) -> str:
    """Run a contract against an execution and show detailed results for each
    constraint — including what was checked, what matched, and what didn't.

    Use this to verify your contract works against real traces before deploying.
    """
    lines = [
        "=" * 60,
        "  CONTRACT DRY RUN",
        f"  Contract: {contract.name} v{contract.version}",
        f"  Execution: {execution.execution_id}",
        "=" * 60,
        "",
    ]

    available_steps = set(execution.step_names())
    available_sources = execution.data_source_names()

    all_passed = True
    for i, constraint in enumerate(contract.constraints):
        result = constraint.evaluate(execution)
        status = "PASS" if result.passed else "FAIL"
        if not result.passed:
            all_passed = False

        lines.append(f"  [{status}] {constraint.name}")

        if result.passed:
            lines.append(f"         OK")
        elif result.violation:
            lines.append(f"         {result.violation.message}")
            if result.violation.evidence:
                for key, val in result.violation.evidence.items():
                    lines.append(f"         {key}: {val}")

        lines.append("")

    # Warnings about potential contract issues
    warnings = _check_contract_coverage(contract, available_steps, available_sources)
    if warnings:
        lines.append("  WARNINGS:")
        lines.append("  " + "-" * 56)
        for w in warnings:
            lines.append(f"    {w}")
        lines.append("")

    lines.append("  " + "-" * 56)
    verdict = "ALL PASSED" if all_passed else "FAILURES DETECTED"
    lines.append(f"  Result: {verdict} ({len(contract.constraints)} constraints checked)")
    lines.append("=" * 60)
    return "\n".join(lines)


def _check_contract_coverage(
    contract: Contract,
    available_steps: set[str],
    available_sources: set[str],
) -> list[str]:
    """Check for potential issues in contract definition against known execution data."""
    warnings: list[str] = []

    for constraint in contract.constraints:
        # Check MustRetrieveFrom for unknown sources
        if hasattr(constraint, "required_sources"):
            for source in constraint.required_sources:
                if source not in available_sources:
                    warnings.append(
                        f"Source '{source}' in {constraint.name} was not found in this "
                        f"execution. Available sources: {sorted(available_sources)}. "
                        f"Check your adapter's tool_source_map."
                    )

        # Check MustIncludeSteps for unknown step names
        if hasattr(constraint, "required_steps"):
            for step in constraint.required_steps:
                if step not in available_steps:
                    warnings.append(
                        f"Step '{step}' in {constraint.name} was not found in this "
                        f"execution. Available steps: {sorted(available_steps)}. "
                        f"Check that step names match your OTEL span names."
                    )

        # Check MustPrecede for unknown step names
        if hasattr(constraint, "before") and hasattr(constraint, "after"):
            for name in (constraint.before, constraint.after):
                if name not in available_steps:
                    warnings.append(
                        f"Step '{name}' in {constraint.name} was not found in this "
                        f"execution. Available steps: {sorted(available_steps)}."
                    )

    return warnings
