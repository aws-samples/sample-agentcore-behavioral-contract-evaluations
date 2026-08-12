# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Core validation engine."""

from agent_validator.contracts.base import Contract
from agent_validator.models import NormalizedExecution, Verdict, VerdictStatus


def validate(contract: Contract, execution: NormalizedExecution) -> Verdict:
    """Run all constraints in a contract against an execution and return a verdict."""
    violations = []
    checks_passed = 0

    for constraint in contract.constraints:
        result = constraint.evaluate(execution)
        if result.passed:
            checks_passed += 1
        elif result.violation:
            violations.append(result.violation)

    errors = [v for v in violations if v.severity == "error"]
    warnings = [v for v in violations if v.severity == "warning"]

    if errors:
        status = VerdictStatus.FAIL
    elif warnings:
        status = VerdictStatus.WARN
    else:
        status = VerdictStatus.PASS

    return Verdict(
        status=status,
        contract_name=contract.name,
        execution_id=execution.execution_id,
        violations=violations,
        checks_performed=len(contract.constraints),
        checks_passed=checks_passed,
    )
