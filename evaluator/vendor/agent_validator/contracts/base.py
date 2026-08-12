# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Base classes for behavioral contracts and constraints."""

from __future__ import annotations

from dataclasses import dataclass

from agent_validator.models import (
    CheckMethod,
    ConstraintType,
    NormalizedExecution,
    Violation,
)


@dataclass
class ConstraintResult:
    """The result of evaluating a single constraint."""

    passed: bool
    violation: Violation | None = None


class Constraint:
    """Base class for all behavioral constraints."""

    name: str = ""
    constraint_type: ConstraintType = ConstraintType.SOURCE
    check_method: CheckMethod = CheckMethod.DETERMINISTIC
    severity: str = "error"

    def evaluate(self, execution: NormalizedExecution) -> ConstraintResult:
        raise NotImplementedError


class Contract:
    """A named, versioned collection of behavioral constraints."""

    def __init__(self, name: str, version: str, constraints: list[Constraint]):
        self.name = name
        self.version = version
        self.constraints = constraints

    def __repr__(self) -> str:
        return f"Contract(name={self.name!r}, version={self.version!r}, constraints={len(self.constraints)})"
