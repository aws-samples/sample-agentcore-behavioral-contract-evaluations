# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Contract registry and loader.

Loads contracts by built-in name or from Python files.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from agent_validator.contracts.base import Contract
from agent_validator.contracts.templates.claims_processing import claims_processing_contract
from agent_validator.contracts.templates.kyc_aml import kyc_aml_contract
from agent_validator.contracts.templates.clinical_decision import clinical_decision_contract

# Built-in contract registry: name -> (contract, description)
BUILTIN_CONTRACTS: dict[str, tuple[Contract, str]] = {
    "claims-processing-v1": (
        claims_processing_contract,
        "HCLS pharmacy claims processing (6 constraints)",
    ),
    "kyc-aml-screening-v1": (
        kyc_aml_contract,
        "FinTech KYC/AML sanctions screening (7 constraints)",
    ),
    "clinical-decision-support-v1": (
        clinical_decision_contract,
        "HCLS clinical decision support (7 constraints)",
    ),
}


def load_contract(name_or_path: str) -> Contract:
    """Load a contract by built-in name, Python file, or YAML file.

    Built-in names: claims-processing-v1, kyc-aml-screening-v1, clinical-decision-support-v1
    Python files: path to a .py file containing a Contract instance.
    YAML files: path to a .yaml/.yml file with contract definition.
    """
    # Try built-in registry first
    if name_or_path in BUILTIN_CONTRACTS:
        return BUILTIN_CONTRACTS[name_or_path][0]

    path = Path(name_or_path)

    # Try as a YAML file
    if path.suffix in (".yaml", ".yml") and path.is_file():
        from agent_validator.generate import load_contract_from_yaml
        contract, _ = load_contract_from_yaml(path)
        return contract

    # Try as a Python file path
    if path.suffix == ".py" and path.is_file():
        return _load_contract_from_file(path)

    # Check if it's a file that doesn't exist
    if path.suffix in (".py", ".yaml", ".yml"):
        raise FileNotFoundError(f"Contract file not found: {name_or_path}")

    available = ", ".join(sorted(BUILTIN_CONTRACTS.keys()))
    raise ValueError(
        f"Unknown contract: {name_or_path!r}. "
        f"Built-in contracts: {available}. "
        f"Or provide a path to a .py or .yaml file containing a Contract."
    )


def _load_contract_from_file(path: Path) -> Contract:
    """Dynamically import a Python file and find the Contract instance."""
    module_name = f"_user_contract_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    # Find Contract instances in the module
    contracts = []
    for attr_name in dir(module):
        obj = getattr(module, attr_name)
        if isinstance(obj, Contract):
            contracts.append((attr_name, obj))

    if not contracts:
        raise ValueError(
            f"No Contract instance found in {path}. "
            f"Define a module-level variable of type Contract."
        )

    if len(contracts) == 1:
        return contracts[0][1]

    # Multiple contracts — look for a conventional name
    for attr_name, obj in contracts:
        if "contract" in attr_name.lower():
            return obj

    # Return the first one
    return contracts[0][1]


def list_contracts() -> list[dict[str, str]]:
    """List all built-in contracts with metadata."""
    result = []
    for name, (contract, description) in sorted(BUILTIN_CONTRACTS.items()):
        result.append({
            "name": name,
            "version": contract.version,
            "constraints": len(contract.constraints),
            "description": description,
        })
    return result
