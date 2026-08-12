# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pre-built behavioral contract for HCLS claims processing agents."""

from agent_validator.contracts.base import Contract
from agent_validator.contracts.constraints import (
    must_contain_citations,
    must_escalate_when,
    must_include_steps,
    must_not_use_only_parametric_knowledge,
    must_precede,
    must_retrieve_from,
)
from agent_validator.models import NormalizedExecution

KNOWN_CLAIM_TYPES = {"pharmacy", "medical", "dental", "vision"}


def _is_unknown_claim_type(execution: NormalizedExecution) -> bool:
    """Check if the claim type in the execution metadata is outside known categories."""
    claim_type = (execution.metadata.get("claim_type") or "").lower().strip()
    return claim_type != "" and claim_type not in KNOWN_CLAIM_TYPES


claims_processing_contract = Contract(
    name="claims-processing-v1",
    version="1.0.0",
    constraints=[
        # Source constraints
        must_retrieve_from(["formulary-db", "coverage-policy"]),
        must_not_use_only_parametric_knowledge(),
        # Step constraints
        must_include_steps(["lookup_formulary", "check_coverage"]),
        must_precede("check_coverage", "render_decision"),
        # Output constraints
        must_contain_citations(min_count=1),
        # Escalation constraints
        must_escalate_when(
            condition_fn=_is_unknown_claim_type,
            description="claim_type not in trained categories",
        ),
    ],
)
