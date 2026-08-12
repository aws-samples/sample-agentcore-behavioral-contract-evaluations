# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""CLI for agent-validate.

Usage:
    agent-validate discover --traces <path> [--source-map <path>]
    agent-validate dry-run --contract <name-or-path> --traces <path> [--source-map <path>] [--metadata key=val]
    agent-validate run --contract <name-or-path> --traces <glob> [--source-map <path>] [--metadata key=val] [--output <path>] [--fail-on-violation]
    agent-validate list-contracts
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

from agent_validator.adapters.otel import OTELAdapter
from agent_validator.audit import create_audit_record, render_audit_report
from agent_validator.discover import describe_execution, dry_run
from agent_validator.models import VerdictStatus
from agent_validator.registry import list_contracts, load_contract
from agent_validator.validator import validate


def _load_source_map(path: str | None) -> dict:
    """Load tool_source_map from a JSON file."""
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        print(f"Error: source map file not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(p) as f:
        return json.load(f)


def _load_traces(pattern: str) -> list[tuple[str, dict]]:
    """Load trace files matching a path or glob pattern.

    Returns list of (filename, trace_dict) tuples.
    """
    paths = sorted(glob.glob(pattern))
    if not paths:
        # Try as a literal path
        p = Path(pattern)
        if p.is_file():
            paths = [str(p)]
        else:
            print(f"Error: no trace files found matching: {pattern}", file=sys.stderr)
            sys.exit(1)

    traces = []
    for path in paths:
        with open(path) as f:
            traces.append((path, json.load(f)))
    return traces


def _parse_metadata(metadata_args: list[str] | None) -> dict:
    """Parse --metadata key=value arguments into a dict."""
    if not metadata_args:
        return {}
    result = {}
    for item in metadata_args:
        if "=" not in item:
            print(f"Error: --metadata must be key=value, got: {item}", file=sys.stderr)
            sys.exit(1)
        key, value = item.split("=", 1)
        # Try to parse as int/float for numeric metadata like risk_score
        try:
            value = int(value)
        except ValueError:
            try:
                value = float(value)
            except ValueError:
                pass
        result[key] = value
    return result


def cmd_discover(args: argparse.Namespace) -> None:
    """Show what's in a trace file."""
    source_map = _load_source_map(args.source_map)
    adapter = OTELAdapter(tool_source_map=source_map)

    traces = _load_traces(args.traces)
    for filename, raw_trace in traces:
        if len(traces) > 1:
            print(f"\n--- {filename} ---\n")
        execution = adapter.normalize(raw_trace)
        print(describe_execution(execution))


def cmd_dry_run(args: argparse.Namespace) -> None:
    """Test a contract against a trace with detailed output."""
    contract = load_contract(args.contract)
    source_map = _load_source_map(args.source_map)
    metadata = _parse_metadata(args.metadata)
    adapter = OTELAdapter(tool_source_map=source_map)

    traces = _load_traces(args.traces)
    for filename, raw_trace in traces:
        if len(traces) > 1:
            print(f"\n--- {filename} ---\n")
        execution = adapter.normalize(raw_trace)
        execution.metadata.update(metadata)
        print(dry_run(contract, execution))


def cmd_run(args: argparse.Namespace) -> None:
    """Validate traces against a contract and produce audit records."""
    contract = load_contract(args.contract)
    source_map = _load_source_map(args.source_map)
    metadata = _parse_metadata(args.metadata)
    adapter = OTELAdapter(tool_source_map=source_map)

    traces = _load_traces(args.traces)
    all_records = []
    counts = {"pass": 0, "fail": 0, "warn": 0}

    for filename, raw_trace in traces:
        execution = adapter.normalize(raw_trace)
        execution.metadata.update(metadata)

        verdict = validate(contract, execution)
        record = create_audit_record(verdict, execution, contract.version)
        all_records.append(record)
        counts[verdict.status.value] += 1

        # Per-trace output
        if args.quiet:
            status_label = verdict.status.value.upper()
            violation_count = len(verdict.violations)
            suffix = f" ({violation_count} violations)" if violation_count else ""
            print(f"  [{status_label}] {filename}{suffix}")
        else:
            if len(traces) > 1:
                print(f"\n--- {filename} ---\n")
            print(render_audit_report(record))

    # Batch summary
    total = len(traces)
    if total > 1 or args.quiet:
        print()
        print(f"{'=' * 60}")
        print(f"  SUMMARY: {total} executions validated")
        print(f"  Contract: {contract.name} v{contract.version}")
        print(f"  Passed: {counts['pass']}  Failed: {counts['fail']}  Warnings: {counts['warn']}")
        print(f"{'=' * 60}")

    # JSON output
    if args.output:
        output_path = Path(args.output)
        output_data = {
            "contract": contract.name,
            "contract_version": contract.version,
            "total": total,
            "passed": counts["pass"],
            "failed": counts["fail"],
            "warnings": counts["warn"],
            "records": [r.to_dict() for r in all_records],
        }
        with open(output_path, "w") as f:
            json.dump(output_data, f, indent=2, default=str)
        print(f"\nAudit records written to {args.output}")

    # Exit code for CI
    if args.fail_on_violation and counts["fail"] > 0:
        sys.exit(1)


def cmd_list_contracts(args: argparse.Namespace) -> None:
    """List available built-in contracts."""
    contracts = list_contracts()
    if not contracts:
        print("No built-in contracts available.")
        return

    print(f"\n{'Name':<32} {'Version':<10} {'Constraints':<13} Description")
    print(f"{'-' * 32} {'-' * 10} {'-' * 13} {'-' * 40}")
    for c in contracts:
        print(f"{c['name']:<32} {c['version']:<10} {c['constraints']:<13} {c['description']}")
    print()


def cmd_generate(args: argparse.Namespace) -> None:
    """Auto-generate a contract from a golden trace."""
    from agent_validator.generate import (
        generate_contract_from_trace,
        generate_source_map_from_trace,
        contract_to_python,
        contract_to_yaml,
    )

    source_map = _load_source_map(args.source_map)
    adapter = OTELAdapter(tool_source_map=source_map)

    traces = _load_traces(args.traces)
    if not traces:
        print("No traces found.", file=sys.stderr)
        sys.exit(1)

    # Use the first trace as the golden example
    filename, raw_trace = traces[0]
    execution = adapter.normalize(raw_trace)

    contract = generate_contract_from_trace(execution, name=args.name)
    detected_source_map = generate_source_map_from_trace(execution)

    print(f"Generated contract from: {filename}")
    print(f"  Name: {contract.name}")
    print(f"  Constraints: {len(contract.constraints)}")
    print(f"  Tool calls found: {[s.name for s in execution.tool_calls()]}")
    print(f"  Data sources found: {sorted(execution.data_source_names())}")
    print()

    if args.output:
        output_path = args.output
        if output_path.endswith(".yaml") or output_path.endswith(".yml"):
            content = contract_to_yaml(contract, detected_source_map or source_map)
        else:
            content = contract_to_python(contract, detected_source_map or source_map)
        with open(output_path, "w") as f:
            f.write(content)
        print(f"Written to: {output_path}")
    else:
        # Print YAML to stdout
        yaml_output = contract_to_yaml(contract, detected_source_map or source_map)
        print("--- Generated Contract (YAML) ---")
        print(yaml_output)
        print("--- Generated Contract (Python) ---")
        python_output = contract_to_python(contract, detected_source_map or source_map)
        print(python_output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-validate",
        description="Behavioral validation for AI agents in regulated environments",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # discover
    p_discover = subparsers.add_parser(
        "discover",
        help="Inspect trace files to see step names, data sources, and structure",
    )
    p_discover.add_argument("--traces", required=True, help="Path or glob pattern for trace files")
    p_discover.add_argument("--source-map", help="Path to JSON source map file")
    p_discover.set_defaults(func=cmd_discover)

    # dry-run
    p_dry_run = subparsers.add_parser(
        "dry-run",
        help="Test a contract against traces with detailed constraint-by-constraint output",
    )
    p_dry_run.add_argument("--contract", required=True, help="Built-in contract name or path to .py file")
    p_dry_run.add_argument("--traces", required=True, help="Path or glob pattern for trace files")
    p_dry_run.add_argument("--source-map", help="Path to JSON source map file")
    p_dry_run.add_argument("--metadata", action="append", help="Execution metadata as key=value (repeatable)")
    p_dry_run.set_defaults(func=cmd_dry_run)

    # run
    p_run = subparsers.add_parser(
        "run",
        help="Validate traces against a contract and produce audit records",
    )
    p_run.add_argument("--contract", required=True, help="Built-in contract name or path to .py file")
    p_run.add_argument("--traces", required=True, help="Path or glob pattern for trace files")
    p_run.add_argument("--source-map", help="Path to JSON source map file")
    p_run.add_argument("--metadata", action="append", help="Execution metadata as key=value (repeatable)")
    p_run.add_argument("--output", help="Path to write JSON audit records")
    p_run.add_argument("--quiet", "-q", action="store_true", help="One-line-per-trace output (for batch runs)")
    p_run.add_argument(
        "--fail-on-violation",
        action="store_true",
        help="Exit with code 1 if any execution fails (for CI)",
    )
    p_run.set_defaults(func=cmd_run)

    # list-contracts
    p_list = subparsers.add_parser(
        "list-contracts",
        help="List available built-in contracts",
    )
    p_list.set_defaults(func=cmd_list_contracts)

    # generate
    p_gen = subparsers.add_parser(
        "generate",
        help="Auto-generate a contract from a golden trace",
    )
    p_gen.add_argument("--traces", required=True, help="Path to a known-good trace file")
    p_gen.add_argument("--source-map", help="Path to JSON source map file")
    p_gen.add_argument("--name", default="auto-generated-v1", help="Contract name")
    p_gen.add_argument("--output", help="Output file path (.py or .yaml)")
    p_gen.set_defaults(func=cmd_generate)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
