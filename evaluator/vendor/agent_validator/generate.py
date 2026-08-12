# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Auto-generate behavioral contracts from golden traces.

Run your agent once on a known-good execution, then generate a contract
that encodes what the agent did as what it MUST do.

Usage:
    from agent_validator.generate import generate_contract_from_trace
    contract = generate_contract_from_trace(execution, name="my-agent-v1")

CLI:
    agent-validate generate --traces golden_trace.json --source-map map.json --name my-agent-v1
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from agent_validator.contracts.base import Contract
from agent_validator.contracts.constraints import (
    MustIncludeSteps,
    MustNotUseOnlyParametricKnowledge,
    MustPrecede,
    MustRetrieveFrom,
    OutputMustContainCitations,
    _CITATION_PATTERNS,
)
from agent_validator.models import NormalizedExecution, StepType


def generate_contract_from_trace(
    execution: NormalizedExecution,
    name: str = "auto-generated-v1",
    version: str = "1.0.0",
) -> Contract:
    """Generate a behavioral contract from a golden trace.

    Analyzes what the agent did and creates constraints that require
    the same behavior on every future execution.
    """
    constraints = []

    # 1. Data sources — require all sources that were consulted
    sources = sorted(execution.data_source_names())
    if sources:
        constraints.append(MustRetrieveFrom(sources))

    # 2. Parametric knowledge — if tools were used, always require tools
    tool_calls = execution.tool_calls()
    if tool_calls:
        constraints.append(MustNotUseOnlyParametricKnowledge())

    # 3. Required steps — all tool calls become required steps
    tool_names = [s.name for s in tool_calls]
    if tool_names:
        constraints.append(MustIncludeSteps(tool_names))

    # 4. Step ordering — preserve the order tools were called in
    for i in range(len(tool_names) - 1):
        constraints.append(MustPrecede(tool_names[i], tool_names[i + 1]))

    # 5. Citations — if the output has citations, require them
    output = execution.output_text or ""
    if output:
        citation_count = 0
        for pattern in _CITATION_PATTERNS:
            citation_count += len(re.findall(pattern, output))
        if citation_count > 0:
            constraints.append(OutputMustContainCitations(min_count=1))

    return Contract(name=name, version=version, constraints=constraints)


def generate_source_map_from_trace(execution: NormalizedExecution) -> dict:
    """Generate a tool_source_map from a golden trace.

    Maps each tool that has data sources to its source identity.
    """
    source_map = {}
    for step in execution.tool_calls():
        if step.data_sources:
            ds = step.data_sources[0]  # primary source
            source_map[step.name] = {
                "source_name": ds.source_name,
                "source_type": ds.source_type,
            }
    return source_map


def contract_to_yaml(contract: Contract, source_map: dict | None = None) -> str:
    """Serialize a contract to YAML format."""
    lines = [
        f"name: {contract.name}",
        f"version: {contract.version}",
        "",
    ]

    if source_map:
        lines.append("source_map:")
        for tool, mapping in source_map.items():
            lines.append(f"  {tool}:")
            lines.append(f"    source_name: {mapping['source_name']}")
            lines.append(f"    source_type: {mapping.get('source_type', 'unknown')}")
        lines.append("")

    lines.append("constraints:")
    for c in contract.constraints:
        if isinstance(c, MustRetrieveFrom):
            lines.append(f"  - type: must_retrieve_from")
            lines.append(f"    sources: {json.dumps(c.required_sources)}")
        elif isinstance(c, MustNotUseOnlyParametricKnowledge):
            lines.append(f"  - type: must_not_use_only_parametric_knowledge")
        elif isinstance(c, MustIncludeSteps):
            lines.append(f"  - type: must_include_steps")
            lines.append(f"    steps: {json.dumps(c.required_steps)}")
        elif isinstance(c, MustPrecede):
            lines.append(f"  - type: must_precede")
            lines.append(f"    before: {c.before}")
            lines.append(f"    after: {c.after}")
        elif isinstance(c, OutputMustContainCitations):
            lines.append(f"  - type: must_contain_citations")
            lines.append(f"    min_count: {c.min_count}")
        lines.append("")

    return "\n".join(lines)


def contract_to_python(contract: Contract, source_map: dict | None = None) -> str:
    """Generate Python code for a contract."""
    lines = [
        '"""Auto-generated behavioral contract."""',
        "",
        "from agent_validator.contracts.base import Contract",
        "from agent_validator.contracts.constraints import (",
    ]

    # Collect needed imports
    imports = set()
    for c in contract.constraints:
        if isinstance(c, MustRetrieveFrom):
            imports.add("must_retrieve_from")
        elif isinstance(c, MustNotUseOnlyParametricKnowledge):
            imports.add("must_not_use_only_parametric_knowledge")
        elif isinstance(c, MustIncludeSteps):
            imports.add("must_include_steps")
        elif isinstance(c, MustPrecede):
            imports.add("must_precede")
        elif isinstance(c, OutputMustContainCitations):
            imports.add("must_contain_citations")

    for imp in sorted(imports):
        lines.append(f"    {imp},")
    lines.append(")")
    lines.append("")

    if source_map:
        lines.append("# Tool-to-source mapping for the OTEL adapter")
        lines.append(f"TOOL_SOURCE_MAP = {json.dumps(source_map, indent=4)}")
        lines.append("")

    lines.append(f'contract = Contract(')
    lines.append(f'    name="{contract.name}",')
    lines.append(f'    version="{contract.version}",')
    lines.append(f'    constraints=[')

    for c in contract.constraints:
        if isinstance(c, MustRetrieveFrom):
            lines.append(f'        must_retrieve_from({json.dumps(c.required_sources)}),')
        elif isinstance(c, MustNotUseOnlyParametricKnowledge):
            lines.append(f'        must_not_use_only_parametric_knowledge(),')
        elif isinstance(c, MustIncludeSteps):
            lines.append(f'        must_include_steps({json.dumps(c.required_steps)}),')
        elif isinstance(c, MustPrecede):
            lines.append(f'        must_precede("{c.before}", "{c.after}"),')
        elif isinstance(c, OutputMustContainCitations):
            lines.append(f'        must_contain_citations(min_count={c.min_count}),')

    lines.append(f'    ],')
    lines.append(f')')
    lines.append("")

    return "\n".join(lines)


def _build_condition(spec: dict) -> tuple:
    """Build a condition callable from a YAML ``when:`` spec.

    Supported condition types (multiple keys AND together):

    - ``metadata``: dict of execution metadata key-value pairs that must all match
    - ``input_matches``: case-insensitive regex searched in the execution input text
    - ``step_present``: a step name (or list of names, any-of) that must appear
    - ``tool_result_matches``: {tool, key, value} -- the named tool must have been
      called and its parsed output must contain key == value

    Design note: gate conditions on facts the agent does not control (the
    request, committed tool results), never on whether the agent felt like
    doing a step -- otherwise the contract lets the agent select its own
    requirements.

    Returns (condition_fn, description).
    """
    known = {"metadata", "input_matches", "step_present", "tool_result_matches"}
    unknown = set(spec) - known
    if unknown:
        raise ValueError(f"Unknown condition type(s) in when: {sorted(unknown)}. Supported: {sorted(known)}")

    preds = []
    descs = []

    if "metadata" in spec:
        match_dict = dict(spec["metadata"])
        preds.append(lambda ex, m=match_dict: all(ex.metadata.get(k) == v for k, v in m.items()))
        descs.append(" AND ".join(f"metadata.{k}={v}" for k, v in match_dict.items()))

    if "input_matches" in spec:
        pattern = re.compile(str(spec["input_matches"]), re.IGNORECASE)
        preds.append(lambda ex, p=pattern: bool(p.search(ex.input_text or "")))
        descs.append(f"input matches /{spec['input_matches']}/")

    if "step_present" in spec:
        names = spec["step_present"]
        if isinstance(names, str):
            names = [names]
        preds.append(lambda ex, ns=list(names): any(n in ex.step_names() for n in ns))
        descs.append(f"step present: {names}")

    if "tool_result_matches" in spec:
        trm = spec["tool_result_matches"]
        tool, key, value = trm["tool"], trm["key"], trm["value"]

        def _tool_result_pred(ex, tool=tool, key=key, value=value):
            for step in ex.steps:
                if step.name == tool and isinstance(step.outputs, dict):
                    if step.outputs.get(key) == value:
                        return True
            return False

        preds.append(_tool_result_pred)
        descs.append(f"{tool}.{key}=={json.dumps(value)}")

    return (lambda ex: all(p(ex) for p in preds)), " AND ".join(descs)


def load_contract_from_yaml(yaml_path: str | Path) -> tuple[Contract, dict]:
    """Load a contract from a YAML file. Returns (contract, source_map).

    Each constraint entry supports two optional fields:

    - ``severity``: "error" (default) or "warning" -- warnings produce a WARN
      verdict instead of FAIL, useful for path-specific expectations.
    - ``when``: a condition spec (see _build_condition). The constraint only
      evaluates when the condition holds; otherwise it is skipped (counts as
      pass). This is how one contract validates multiple legitimate execution
      paths.
    """
    import yaml

    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    from agent_validator.contracts.constraints import (
        must_contain_citations,
        must_include_steps,
        must_not_include_steps,
        must_not_use_only_parametric_knowledge,
        must_precede,
        must_retrieve_from,
        when,
    )

    constraints = []
    for c in data.get("constraints", []):
        ctype = c["type"]
        severity = c.get("severity", "error")
        if ctype == "must_retrieve_from":
            constraint = must_retrieve_from(c["sources"], severity=severity)
        elif ctype == "must_not_use_only_parametric_knowledge":
            constraint = must_not_use_only_parametric_knowledge(severity=severity)
        elif ctype == "must_include_steps":
            constraint = must_include_steps(c["steps"], severity=severity)
        elif ctype == "must_not_include_steps":
            constraint = must_not_include_steps(c["steps"], severity=severity)
        elif ctype == "must_precede":
            constraint = must_precede(c["before"], c["after"], severity=severity)
        elif ctype == "must_contain_citations":
            constraint = must_contain_citations(min_count=c.get("min_count", 1), severity=severity)
        else:
            raise ValueError(f"Unknown constraint type: {ctype}")

        if "when" in c:
            condition_fn, description = _build_condition(c["when"])
            constraint = when(condition_fn, constraint, description)

        constraints.append(constraint)

    contract = Contract(
        name=data.get("name", "unnamed"),
        version=data.get("version", "1.0.0"),
        constraints=constraints,
    )
    source_map = data.get("source_map", {})
    return contract, source_map
