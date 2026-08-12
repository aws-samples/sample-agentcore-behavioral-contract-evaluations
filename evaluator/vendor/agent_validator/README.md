# agent_validator — Source Reference

## What This Library Does

Takes OTEL traces from an AI agent execution, checks them against a behavioral
contract, and produces a verdict with audit evidence.

```python
from agent_validator import validate, create_audit_record, render_audit_report
from agent_validator.adapters.otel import OTELAdapter

# 1. Normalize traces
adapter = OTELAdapter(tool_source_map={
    "lookup_formulary": {"source_name": "formulary-db", "source_type": "database"},
})
execution = adapter.normalize(otel_trace_json)

# 2. Validate
verdict = validate(contract=my_contract, execution=execution)

# 3. Produce audit evidence
record = create_audit_record(verdict, execution, contract_version="1.0.0")
print(render_audit_report(record))
```

---

## Module Map

```
agent_validator/
├── __init__.py              <- Public API: validate, create_audit_record, describe_execution, dry_run, save_audit_record, save_batch_report
├── cli.py                   <- CLI entrypoint: agent-validate command (discover, run, dry-run, list-contracts)
├── registry.py              <- Contract loader: built-in registry + dynamic .py file import
├── models.py                <- Core data models (the foundation everything else uses)
├── validator.py             <- Validation engine (runs constraints, produces verdicts)
├── audit.py                 <- Audit record creation and rendering
├── store.py                 <- Audit record persistence (local files, S3)
├── monitor.py               <- Production monitoring (CloudWatch, directory watcher)
├── compliance.py            <- Compliance tracking, drift detection, threshold alerts
├── generate.py              <- Auto-generate contracts from golden traces (YAML + Python)
├── discover.py              <- Discovery and dry-run helpers for building contracts
├── adapters/
│   ├── base.py              <- TraceAdapter abstract base class
│   └── otel.py              <- OTEL adapter (normalizes OTLP JSON spans)
└── contracts/
    ├── base.py              <- Contract and Constraint base classes
    ├── constraints.py       <- Built-in constraint implementations and builders
    └── templates/
        ├── claims_processing.py   <- HCLS claims processing contract
        ├── kyc_aml.py             <- FinTech KYC/AML screening contract
        └── clinical_decision.py   <- HCLS clinical decision support contract
```

---

## models.py — Core Data Models

Everything flows through these types.

**Execution model** (what the adapter produces):

| Class | Purpose |
|---|---|
| `NormalizedExecution` | A complete agent execution: steps, inputs, outputs, data sources, metadata |
| `ExecutionStep` | One step in an execution: tool call, model call, retrieval, or custom |
| `DataSourceRef` | A reference to an external data source consulted during a step |
| `StepType` | Enum: `model_call`, `tool_call`, `retrieval`, `human_review`, `custom` |

**Validation model** (what the validator produces):

| Class | Purpose |
|---|---|
| `Verdict` | The result of validation: pass/fail/warn, violation list, check counts |
| `Violation` | A single constraint failure: name, type, method, message, severity, evidence |
| `AuditRecord` | Verdict + execution summary + evidence package, serializable to JSON |
| `VerdictStatus` | Enum: `pass`, `fail`, `warn` |
| `ConstraintType` | Enum: `source`, `step`, `output`, `escalation` |
| `CheckMethod` | Enum: `deterministic`, `heuristic`, `llm_judge` |

`NormalizedExecution` has convenience methods:

```python
execution.tool_calls()        # steps where step_type == TOOL_CALL
execution.model_calls()       # steps where step_type == MODEL_CALL
execution.retrievals()        # steps where step_type == RETRIEVAL
execution.step_names()        # ordered list of step names
execution.all_data_sources()  # all DataSourceRef across all steps
execution.data_source_names() # set of source name strings
```

---

## cli.py — Command Line Interface

The `agent-validate` command. Four subcommands:

```bash
agent-validate list-contracts                    # show built-in contracts
agent-validate discover --traces T --source-map M    # inspect traces
agent-validate dry-run --contract C --traces T ...   # test contract
agent-validate run --contract C --traces T ...       # validate + audit
```

Key flags for `run`:
- `--quiet` / `-q` — one line per trace (for batch runs)
- `--output report.json` — write JSON audit records
- `--fail-on-violation` — exit code 1 on any failure (for CI)
- `--metadata key=value` — inject execution metadata (repeatable)

---

## registry.py — Contract Loader

Loads contracts by name or from files:

```python
from agent_validator.registry import load_contract, list_contracts

contract = load_contract("claims-processing-v1")       # built-in
contract = load_contract("./my_contract.py")           # from file

for c in list_contracts():
    print(c["name"], c["description"])
```

When loading from a `.py` file, it finds the `Contract` instance in the module
(prefers variables with "contract" in the name if multiple exist).

---

## validator.py — Validation Engine

One function. Iterates constraints, collects results, determines verdict.

```python
verdict = validate(contract, execution)
# verdict.status: VerdictStatus.PASS | FAIL | WARN
# verdict.violations: list[Violation]
# verdict.checks_performed: int
# verdict.checks_passed: int
```

Logic: if any violation has `severity="error"` the verdict is FAIL. If only
`severity="warning"` violations exist, it's WARN. Otherwise PASS.

---

## audit.py — Audit Records

```python
record = create_audit_record(verdict, execution, contract_version="1.0.0")
record.to_dict()              # JSON-serializable dict
render_audit_report(record)   # human-readable terminal output
```

The audit record bundles:
- Verdict (status, checks, violations)
- Execution summary (ID, source, steps, tool calls, data sources, input/output preview)
- Evidence package (violation details with proof)
- Metadata (audit ID, timestamp, contract version)

---

## store.py — Audit Record Persistence

```python
from agent_validator import save_audit_record, save_batch_report

# Save individual audit record
path = save_audit_record(record, "./results")
# -> results/pass_abc123_20260324T013831.json

# Save batch report with summary
path = save_batch_report(records, "claims-processing-v1", "1.0.0", "./results")
# -> results/report_claims-processing-v1_20260324T013831.json

# Save to S3
from agent_validator.store import save_to_s3
s3_key = save_to_s3(record, bucket="compliance-bucket", prefix="audit-records")
# -> s3://compliance-bucket/audit-records/2026/03/24/pass_abc123_<uuid>.json
```

---

## monitor.py — Production Monitoring

Continuously validates new traces as they arrive.

```python
from agent_validator import monitor_directory, monitor_cloudwatch

# Watch a directory for new trace files
stats = monitor_directory(
    contract, adapter,
    watch_dir="./incoming_traces",
    output_dir="./audit_records",
    metadata={"claim_type": "pharmacy"},
    on_violation=lambda record: alert_slack(record),  # custom handler
)
# stats.compliance_rate -> 0.95

# Watch CloudWatch aws/spans (for AgentCore)
stats = monitor_cloudwatch(
    contract, adapter,
    region="us-west-2",
    poll_interval=30,
    output_dir="./audit_records",
)
```

`MonitorStats` tracks: `traces_validated`, `passed`, `failed`, `warned`, `errors`, `compliance_rate`.

---

## compliance.py — Compliance Tracking and Drift Detection

```python
from agent_validator import ComplianceTracker, CompliancePolicy

tracker = ComplianceTracker(
    policy=CompliancePolicy(
        overall_threshold=0.95,
        min_window_size=10,
        constraint_thresholds={"must_include_steps(...)": 0.99},
    ),
    window_size=100,
    on_alert=lambda alert: print(f"DRIFT: {alert.message}"),
)

# Feed verdicts from the monitor or validator
alerts = tracker.record(verdict)

# Check state
tracker.overall_compliance      # 0.95
tracker.constraint_compliance   # per-constraint rates
tracker.summary()               # full state dict
```

Features:
- Sliding window (old failures drop off, compliance recovers)
- Per-constraint threshold overrides
- Minimum window size (avoids alerting on 1 failure out of 3 executions)
- `DriftAlert` objects with timestamp, rate, threshold, message

---

## discover.py — Discovery and Dry Run

Helpers for building and testing contracts before deploying them.

```python
from agent_validator import describe_execution, dry_run

# Show what's in a trace: step names, data sources, step order, output
print(describe_execution(execution))

# Run a contract and show detailed results + warnings about mismatches
print(dry_run(contract, execution))
```

`dry_run` warns about potential issues:
- Step names in constraints that don't appear in the execution
- Source names in constraints that don't appear in the execution
- Suggests checking the adapter's `tool_source_map`

---

## adapters/ — Trace Normalization

### base.py

Abstract interface every adapter implements:

```python
class TraceAdapter(ABC):
    def normalize(self, raw_trace: Any) -> NormalizedExecution: ...
    def normalize_many(self, raw_traces: list[Any]) -> list[NormalizedExecution]: ...
```

### otel.py — OTEL Adapter

Normalizes OTLP JSON exports into `NormalizedExecution` records.

```python
adapter = OTELAdapter(tool_source_map={
    "lookup_formulary": {"source_name": "formulary-db", "source_type": "database"},
    "check_coverage":   {"source_name": "coverage-policy", "source_type": "api"},
})
execution = adapter.normalize(raw_trace_dict)
```

**`tool_source_map`** is the critical configuration. It bridges tool names (from
OTEL spans) to logical data source names (used in contracts). Without it, source
constraints can't work because the adapter doesn't inherently know which external
system a tool talks to.

The adapter handles:
- OTLP JSON format (nested `resourceSpans > scopeSpans > spans`)
- Flat span list format (e.g., from `InMemorySpanExporter`)
- CloudWatch `aws/spans` format (dict-style attributes from Bedrock AgentCore)
- GenAI semantic conventions: `gen_ai.operation.name`, `gen_ai.tool.name`, etc.
- Root span detection (by missing parent or earliest timestamp)
- Input/output extraction from span attributes (`gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.user.message`, `gen_ai.choice`)
- Input/output extraction from span events (OTEL GenAI v1.36.0 convention — content in events, not attributes)
- Bedrock Knowledge Base detection via `aws.bedrock.knowledge_base.id`

**Validated against real traces** from Strands Agents, both locally and on Bedrock AgentCore.

---

## contracts/ — Behavioral Contracts

### base.py

```python
class Constraint:
    def evaluate(self, execution: NormalizedExecution) -> ConstraintResult: ...

class Contract:
    def __init__(self, name: str, version: str, constraints: list[Constraint]): ...
```

### constraints.py — Built-in Constraints

Six constraint types with builder functions:

| Builder | Type | Method | What it checks |
|---|---|---|---|
| `must_retrieve_from(sources)` | source | deterministic | Each listed data source was consulted |
| `must_not_use_only_parametric_knowledge()` | source | deterministic | At least one tool call or retrieval occurred |
| `must_include_steps(steps)` | step | deterministic | Each listed step name appears in execution |
| `must_precede(before, after)` | step | deterministic | Step A occurs before step B |
| `must_contain_citations(min_count)` | output | heuristic | Output contains citation patterns |
| `must_escalate_when(fn, desc)` | escalation | deterministic | When condition is true, escalation step exists |
| `when(condition, constraint)` | (wraps any) | (wraps any) | Only evaluates inner constraint when condition is met |

Citation patterns recognized: `Section 4.2.1`, `Policy XYZ`, `[1]`, `(Ref: ...)`,
`Article 5`, `31 CFR 1010.230`, `Directive (EU) 2024/1640`, `Reg. 33`, `Art. 28`.

### Writing a custom constraint

```python
class MyConstraint(Constraint):
    name = "my_custom_check"
    constraint_type = ConstraintType.OUTPUT
    check_method = CheckMethod.DETERMINISTIC

    def evaluate(self, execution: NormalizedExecution) -> ConstraintResult:
        if some_condition(execution):
            return ConstraintResult(passed=True)
        return ConstraintResult(
            passed=False,
            violation=Violation(
                constraint_name=self.name,
                constraint_type=self.constraint_type,
                check_method=self.check_method,
                message="What went wrong",
                severity="error",
                evidence={"key": "value"},
            ),
        )
```

### templates/ — Domain-Specific Contracts

Pre-built contracts for regulated scenarios. Use as-is or as starting points.

| Template | Domain | Constraints | Key checks |
|---|---|---|---|
| `claims_processing.py` | HCLS | 6 | Formulary lookup, coverage check, policy citations, unknown claim escalation |
| `kyc_aml.py` | FinTech | 7 | Sanctions screening, jurisdiction rules, regulatory citations, high-risk escalation |
| `clinical_decision.py` | HCLS | 7 | Drug interactions, clinical guidelines, guideline citations, dangerous interaction escalation |

---

## Data Flow

```
OTEL spans (JSON)
    │
    ▼
OTELAdapter.normalize()          <- adapters/otel.py
    │
    ▼
NormalizedExecution               <- models.py
    │
    ├──► describe_execution()     <- discover.py (optional: inspect before writing contract)
    │
    ├──► dry_run(contract, ...)   <- discover.py (optional: test contract before deploying)
    │
    ▼
validate(contract, execution)     <- validator.py
    │
    ▼
Verdict                           <- models.py
    │
    ▼
create_audit_record(...)          <- audit.py
    │
    ▼
AuditRecord                       <- models.py
    │
    ├──► .to_dict()               <- JSON export for storage/compliance
    └──► render_audit_report()    <- human-readable terminal output
```
