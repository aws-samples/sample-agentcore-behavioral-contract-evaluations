# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pre-built behavioral contract for FinTech KYC/AML compliance agents."""

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

HIGH_RISK_THRESHOLD = 70


def _is_high_risk(execution: NormalizedExecution) -> bool:
    """Check if the customer's risk score exceeds the threshold for mandatory human review."""
    risk_score = execution.metadata.get("risk_score")
    if risk_score is None:
        return False
    return int(risk_score) >= HIGH_RISK_THRESHOLD


kyc_aml_contract = Contract(
    name="kyc-aml-screening-v1",
    version="1.0.0",
    constraints=[
        # Source constraints — agent must check real sanctions and jurisdiction data
        must_retrieve_from(["sanctions-db", "jurisdiction-rules"]),
        must_not_use_only_parametric_knowledge(),

        # Step constraints — must screen before determining, must verify jurisdiction
        must_include_steps(["screen_sanctions", "verify_jurisdiction"]),
        must_precede("screen_sanctions", "render_determination"),
        must_precede("verify_jurisdiction", "render_determination"),

        # Output constraints — determination must cite regulatory basis
        must_contain_citations(min_count=1),

        # Escalation constraints — high-risk customers must go to human review
        must_escalate_when(
            condition_fn=_is_high_risk,
            description=f"risk_score >= {HIGH_RISK_THRESHOLD}",
        ),
    ],
)
