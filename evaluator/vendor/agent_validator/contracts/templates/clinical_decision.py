# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pre-built behavioral contract for HCLS clinical decision support agents."""

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


def _has_known_interaction(execution: NormalizedExecution) -> bool:
    """Check if a drug interaction was detected in tool outputs, requiring escalation."""
    for step in execution.steps:
        if step.outputs and isinstance(step.outputs, dict):
            if step.outputs.get("interaction_found") is True:
                return True
            severity = step.outputs.get("interaction_severity", "")
            if severity in ("major", "contraindicated"):
                return True
    return False


clinical_decision_contract = Contract(
    name="clinical-decision-support-v1",
    version="1.0.0",
    constraints=[
        # Source constraints — must use approved clinical data, not training knowledge
        must_retrieve_from(["drug-interaction-db", "clinical-guidelines"]),
        must_not_use_only_parametric_knowledge(),

        # Step constraints — must check interactions before recommending
        must_include_steps(["check_drug_interactions", "lookup_clinical_guidelines"]),
        must_precede("check_drug_interactions", "render_recommendation"),
        must_precede("lookup_clinical_guidelines", "render_recommendation"),

        # Output constraints — recommendations must cite guideline sources
        must_contain_citations(min_count=1),

        # Escalation — major or contraindicated interactions require clinician review
        must_escalate_when(
            condition_fn=_has_known_interaction,
            description="major or contraindicated drug interaction detected",
        ),
    ],
)
