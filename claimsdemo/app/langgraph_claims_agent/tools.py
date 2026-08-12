"""Simulated tools for the LangGraph claims processing demo agent.

Identical tool names, signatures, and data to the Strands agent
(app/claims_agent/tools.py) -- the same behavioral contract validates both.
All data is synthetic.
"""

from langchain.tools import tool

FORMULARY_DB = {
    "lisinopril": {
        "status": "covered",
        "tier": 1,
        "pa_required": False,
        "policy_section": "Section 4.2.1",
        "drug_class": "ACE Inhibitor",
    },
    "ozempic": {
        "status": "covered",
        "tier": 3,
        "pa_required": True,
        "policy_section": "Section 4.3.7",
        "drug_class": "GLP-1 Receptor Agonist",
    },
    "metformin": {
        "status": "covered",
        "tier": 1,
        "pa_required": False,
        "policy_section": "Section 4.1.2",
        "drug_class": "Biguanide",
    },
    "experimental-drug-x": {
        "status": "not_covered",
        "tier": None,
        "pa_required": False,
        "policy_section": "Section 6.1",
        "drug_class": "Experimental",
    },
}

COVERAGE_DB = {
    "MEM-001": {
        "coverage_active": True,
        "plan_type": "PPO",
        "deductible_met": True,
        "copay_amount": 10.00,
        "restrictions": [],
    },
    "MEM-002": {
        "coverage_active": True,
        "plan_type": "HMO",
        "deductible_met": False,
        "copay_amount": 35.00,
        "restrictions": ["prior_auth_required"],
    },
    "MEM-003": {
        "coverage_active": False,
        "plan_type": "PPO",
        "deductible_met": False,
        "copay_amount": 0,
        "restrictions": ["coverage_expired"],
    },
}


@tool
def lookup_formulary(drug_name: str) -> dict:
    """Look up a drug in the formulary database.

    Returns coverage status, tier, prior auth requirements, and policy section.
    """
    result = FORMULARY_DB.get(drug_name.lower())
    if not result:
        return {"status": "not_found", "drug_name": drug_name, "source": "formulary-db"}
    return {**result, "drug_name": drug_name, "source": "formulary-db"}


@tool
def check_coverage(member_id: str, drug_name: str) -> dict:
    """Check member coverage for a specific drug.

    Returns plan details, deductible status, copay, and any restrictions.
    """
    member = COVERAGE_DB.get(member_id)
    if not member:
        return {"error": f"Member {member_id} not found", "source": "coverage-policy"}
    return {**member, "member_id": member_id, "drug_name": drug_name, "source": "coverage-policy"}


@tool
def render_decision(
    member_id: str,
    drug_name: str,
    decision: str,
    reasoning: str,
    policy_section: str,
) -> dict:
    """Render the final claims decision with a policy citation."""
    return {
        "decision": decision,
        "member_id": member_id,
        "drug_name": drug_name,
        "reasoning": reasoning,
        "citations": [policy_section],
        "source": "decision-engine",
    }
