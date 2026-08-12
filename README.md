# Behavioral Contract Evaluations for Amazon Bedrock AgentCore

Run **process-correctness validation** as a custom code-based evaluator in [Amazon Bedrock AgentCore Evaluations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluations.html).

> **Note:** This is sample code for demonstration and educational purposes. It has not been thoroughly tested, secured, or optimized for production use. Before using any part of it in a production environment, apply your own security review, testing, and operational hardening.

AgentCore's built-in evaluators judge *output quality* (helpfulness, correctness). This sample adds the missing dimension for regulated workloads: **did the agent follow the correct process?** Which data sources were consulted, which steps ran, in what order, and does the output cite its sources — validated deterministically on every execution, with results in CloudWatch GenAI Observability next to the built-in metrics.

## The case this catches

Both runs below answer the same pharmacy claim. Both produce a fluent, plausible answer with a policy citation. The LLM judge cannot tell them apart — in this validated run it scored the lazy agent *higher* — the behavioral contract can. This result is reproduced live by the walkthrough below:

| | Compliant agent | Lazy agent (answers from memory, no tools) |
|---|---|---|
| `Builtin.Helpfulness` (LLM judge) | 0.83 "Very Helpful" | **1.00 "Above And Beyond"** |
| `claims_contract` (this sample) | **PASS** 8/8 checks | **FAIL** 4/8 checks |

The lazy agent's violations, named in the evaluator explanation:

```
Contract claims-processing-v1: 4/8 checks passed.
[x] must_not_use_only_parametric_knowledge: Agent answered using only parametric knowledge
[x] must_retrieve_from(['coverage-policy-api']): Required data sources not consulted
[x] must_include_steps(['check_coverage', 'render_decision']): Required steps missing
[x] must_precede(check_coverage, render_decision): Preceding step not found
audit_id=73dc2af1-...
```

The lazy agent fabricated a plausible "Section 4.2.1" citation in its answer. Output inspection alone cannot catch this — only comparing the output against what the agent actually did can.

And because the contract validates the *process*, not the framework, the same contract validates two implementations of this agent: the sample deploys the claims agent in both **Strands** and **LangGraph**, and the identical YAML produces the identical verdicts for both (see [Same contract, different framework](#same-contract-different-framework)).

## Architecture

```
User ──► Claims Agent (AgentCore Runtime, Strands)
              │ OTEL spans (automatic via AgentCore Observability)
              ▼
         CloudWatch (aws/spans)
              │
              ├──► on-demand:  agentcore run eval / Evaluate API
              └──► online:     OnlineEvaluationConfig (sampled live traffic)
                        │  evaluationInput.sessionSpans
                        ▼
         Contract Evaluator (AWS Lambda)
           span_shim  ──►  OTELAdapter.normalize  ──►  validate(contract)
                        │                                   │
                        ▼                                   ▼
         {label, value, explanation}              AuditRecord ──► S3
                        │
                        ▼
         Results log group + GenAI Observability ► Evaluations tab
```

The Lambda wraps an Apache-2.0 licensed behavioral-contract validation engine, included in the sample under [`evaluator/vendor/`](evaluator/vendor/README.md) so everything runs self-contained — no external packages beyond `pyyaml`. The contract is YAML configuration, not code — including support for multiple legitimate execution paths via conditional constraints:

```yaml
constraints:
  # Unconditional core: holds on EVERY valid path
  - type: must_not_use_only_parametric_knowledge
  - type: must_include_steps
    steps: ["check_coverage", "render_decision"]
  - type: must_precede
    before: check_coverage
    after: render_decision

  # Only required when coverage is active (expired members get a fast deny)
  - type: must_include_steps
    steps: ["lookup_formulary"]
    when:
      tool_result_matches:
        tool: check_coverage
        key: coverage_active
        value: true
```

## What's in the sample

| Path | Purpose |
|---|---|
| `claimsdemo/` | AgentCore CLI project: the demo claims agent in two implementations (Strands and LangGraph) + evaluator/online-eval config, deployed via CDK |
| `evaluator/` | The contract evaluator Lambda: `handler.py` (glue), `span_shim.py` (CloudWatch spans → OTLP), `contracts/claims.yaml` |
| `evaluator/vendor/` | Vendored Apache-2.0 licensed libraries: the contract engine and the OTEL trace normalization layer (see [vendor README](evaluator/vendor/README.md) and [THIRD-PARTY-LICENSES](THIRD-PARTY-LICENSES)) |
| `scripts/` | Pre-flight environment checks, deploy the Lambda, invoke the agent in compliant/lazy mode, generate contracts from golden sessions, clean up |
| `tests/` | Handler unit tests against real captured AgentCore evaluator payloads — run with zero AWS access |

## Prerequisites

- AWS account with credentials configured, in an [AgentCore region](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html)
- Amazon Bedrock model access to **Anthropic Claude Sonnet 5** — the demo agent calls `global.anthropic.claude-sonnet-5` (Bedrock console → **Model access**, in your region). Deployment succeeds without it, but every agent invocation fails — enable it first
- The account/region [CDK-bootstrapped](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping.html) — one-time `npx cdk bootstrap`; the first `agentcore deploy` requires it
- Node.js 20+ and the [AgentCore CLI](https://www.npmjs.com/package/@aws/agentcore) >= 0.21: `npm install -g @aws/agentcore`
- Python 3.11+ with `boto3` and `pyyaml` (`pip install boto3 pyyaml`) for the helper scripts

Verify all of it in one shot before deploying anything:

```bash
python scripts/preflight.py
```

This checks credentials, AgentCore region availability, Bedrock model access (a 1-token test call, well under $0.01), CDK bootstrap, and CLI versions — and prints the exact fix for anything missing.

## Quickstart

All commands assume `AWS_REGION` is set to your AgentCore region (this walkthrough was validated in `us-west-2`).

### 1. Deploy the agent

```bash
cd claimsdemo
cp agentcore/aws-targets.json.example agentcore/aws-targets.json
# edit agentcore/aws-targets.json: set your account ID and region
agentcore deploy --yes          # CDK stack: runtime + observability
```

Wait ~10 minutes after first deploy for CloudWatch Transaction Search to activate (step 4 discovers the runtime ARN automatically, so no need to note it).

### 2. Deploy the contract evaluator Lambda

```bash
cd ..
python scripts/deploy_evaluator_lambda.py
```

This creates the Lambda, its least-privilege IAM role, and an S3 audit bucket (`--no-audit` to skip). The deployment package is built entirely from this repository: the vendored libraries plus `pyyaml`.

### 3. Register the evaluator

```bash
cd claimsdemo
agentcore add evaluator --name claims_contract --level TRACE \
  --type code-based --lambda-arn <LAMBDA_ARN> --timeout 60
agentcore deploy --yes
```

### 4. Generate a compliant and a lazy execution

```bash
cd ..
python scripts/invoke_agent.py --mode both
```

The script discovers the runtime ARN from the CDK stack (pass `--runtime-arn` to override) and, after both runs, prints the exact `agentcore run eval` commands for step 5 with the session IDs pre-filled. Wait 2–5 minutes for spans to reach CloudWatch.

### 5. Run the side-by-side evaluation

```bash
cd claimsdemo
agentcore run eval --runtime claims_agent \
  --evaluator claims_contract --evaluator "Builtin.Helpfulness" \
  --session-id <COMPLIANT_SESSION>

agentcore run eval --runtime claims_agent \
  --evaluator claims_contract --evaluator "Builtin.Helpfulness" \
  --session-id <LAZY_SESSION>
```

Expected: the compliant session passes the contract 8/8; the lazy session scores at least as well on Helpfulness but fails the contract with named violations. To see the multiple-paths behavior, also invoke with member `MEM-003` (expired coverage) in compliant mode — the agent legitimately skips the formulary lookup and still passes 8/8, because the formulary constraints are gated on the coverage check result.

To run the same demo against the LangGraph implementation, add `--runtime langgraph_claims_agent` to the invoke in step 4 — everything else, including the contract, is identical (see [Same contract, different framework](#same-contract-different-framework)).

### 6. Continuous monitoring (online evaluation)

```bash
agentcore add online-eval --name contract_monitoring --runtime claims_agent \
  --evaluator claims_contract "Builtin.Helpfulness" \
  --sampling-rate 100 --enable-on-create
agentcore deploy --yes
```

Every live invocation is now evaluated automatically. Results land in:
- Log group `/aws/bedrock-agentcore/evaluations/results/<config-id>`
- CloudWatch Console → **GenAI Observability → Bedrock AgentCore →** your agent → **Evaluations** tab

For compliance use cases keep sampling at 100% — a sampled audit trail is a partial audit trail.

### 7. Audit records

Every evaluation writes a full evidence package to S3, keyed by verdict:

```bash
aws s3 ls s3://agentcore-contract-audit-<ACCOUNT>-<REGION>/audit/ --recursive
#   audit/PASS/6a5a695903f7e90d....json
#   audit/FAIL/6a5a6994734087....json
```

Each record contains the verdict, per-constraint evidence (what was required, found, missing), contract version, and execution summary — the trail a compliance reviewer needs, linked from the dashboard via the `audit_id` in each explanation.

## Writing contracts

A contract is a YAML file in `evaluator/contracts/` — see [claims.yaml](evaluator/contracts/claims.yaml) for the complete working example this walkthrough uses. Contracts are configuration: adding or changing one is a Lambda redeploy, never a code change.

### Constraint reference

| Constraint | Validates | Parameters |
|---|---|---|
| `must_retrieve_from` | Each named data source was consulted (via the `source_map` tool mapping) | `sources`: list of data source names |
| `must_not_use_only_parametric_knowledge` | The agent made at least one tool call or retrieval — it didn't answer purely from training data | none |
| `must_include_steps` | Every named step (tool) appears in the execution | `steps`: list of step names |
| `must_precede` | Step A ran before step B (e.g. "look up the policy *before* rendering the decision") | `before`, `after` |
| `must_not_include_steps` | None of the named steps may appear (e.g. "on the escalation path, do NOT render a decision") | `steps`: list of forbidden step names |
| `must_contain_citations` | The final output contains at least N citation references (Section X.Y, [1], 31 CFR 1010.230, Art. 28, ...) | `min_count` (default 1) |

Every constraint also accepts:

- **`severity`**: `error` (default) or `warning` — warnings produce a WARN verdict instead of FAIL, useful for expectations that shouldn't block.
- **`when`**: a condition that gates the constraint — see the next section.

Two supporting fields:

- **`source_map`** bridges "tool X was called" to "data source Y was consulted" — it's how `must_retrieve_from` knows that a `lookup_formulary` span means the formulary database was checked.
- **`applies_to`** routes the contract to its agent(s) — see [One evaluator, many agents](#one-evaluator-many-agents).

### Multiple valid paths: conditional constraints

Real agents have more than one legitimate execution path. In this sample, a member with active coverage requires the full formulary process, but a member with **expired coverage gets a fast deny — no formulary lookup needed**. A contract that hard-requires `lookup_formulary` would false-alarm on every legitimate fast deny.

Conditional constraints solve this: a constraint with a `when:` block only evaluates when the condition holds, and is skipped (counts as pass) otherwise. Condition types, AND-ed when combined:

| Condition | Fires when | Example |
|---|---|---|
| `tool_result_matches` | A tool was called and its parsed output contains `key == value` | `{tool: check_coverage, key: coverage_active, value: true}` |
| `input_matches` | Case-insensitive regex found in the request text | `"pharmacy claim"` |
| `step_present` | A named step (or any of a list) appears in the execution | `"flag_for_human_review"` |
| `metadata` | Execution metadata key-values all match | `{claim_type: pharmacy}` |

Two design rules keep branching contracts honest:

1. **Gate on facts the agent doesn't control.** Condition on the request or on committed tool results (`check_coverage` returned `coverage_active: false`), never on whether the agent felt like doing a step — otherwise a lazy agent selects its own requirements.
2. **Keep a strong unconditional core.** The lazy agent that skips `check_coverage` entirely dodges every coverage-gated constraint — and is caught by the unconditional core (`must_include_steps: [check_coverage]`, `must_not_use_only_parametric_knowledge`). Verified live in this sample: the fast-deny path passes 8/8 while the lazy agent fails 4/8.

See [claims.yaml](evaluator/contracts/claims.yaml) for the complete branching contract this walkthrough runs.

### When contracts fit — and when to write code instead

The value of behavioral contracts is proportional to the gap between how messy your inputs are and how rigid your process must be. Wide gap (natural-language requests in, regulated process out): high value — that's this sample's domain. Narrow gap (structured inputs, fully specifiable process): write deterministic code instead; an agent is the wrong architecture there. No process requirements at all: there's no contract to write.

As agent behavior gets more varied, climb the abstraction ladder: a few branches → `when:` conditions; dozens of paths → multiple routed contracts; high variety → path-independent invariants (`must_not_*`, escalation and precondition rules), whose count stays flat as variety grows. If your contract starts re-specifying every step and branch, that's a signal the process was fully specifiable all along — reconsider whether an LLM should be deciding it.

Even where a deterministic implementation is conceivable, contracts add what code can't: drift detection for a component that changes behavior when models or prompts change, an independent checker (the same reason we write tests), and versioned audit evidence you can hand a reviewer.

### The fast path: generate a contract from a golden session

You don't have to write contracts by hand. Run your agent once on a known-good case, then generate a baseline contract from what it did:

```bash
# 1. Invoke your agent on a golden case, note the session ID
# 2. Wait 2-5 min for CloudWatch ingestion, then:
python scripts/generate_contract.py \
  --session-id <GOLDEN_SESSION_ID> \
  --name my-agent-v1 \
  --source-map my_source_map.json \
  --output evaluator/contracts/my_agent.yaml
```

The generator encodes the golden execution as requirements — every data source consulted becomes `must_retrieve_from`, every tool call becomes a required step, the observed ordering becomes `must_precede` chains, citations in the output become `must_contain_citations` — and adds an `applies_to` pattern for the agent that produced the session. Review the YAML before deploying: you decide which constraints generalize beyond the one golden case (ordering chains are the usual candidates for pruning).

## One evaluator, many agents

You don't need one Lambda per agent. The evaluator loads **every** YAML in `evaluator/contracts/` at cold start and routes each evaluation to the right contract(s) using the service name AgentCore stamps on every span (`claimsdemo_claims_agent.DEFAULT`):

```yaml
# evaluator/contracts/claims.yaml
applies_to:
  - "*claims_agent*"

# evaluator/contracts/kyc.yaml
applies_to:
  - "*kyc_agent*"
```

Routing rules:

- Every contract whose `applies_to` glob matches the trace's service name is evaluated. Multiple matches aggregate (worst verdict wins) — useful for an org-wide baseline contract (`applies_to: ["*"]`) layered under domain contracts.
- A contract without `applies_to` acts as a catch-all only when it's the sole contract deployed, so the single-agent case needs no routing config.
- If no contract matches, the evaluator returns an `UNKNOWN_AGENT` error naming the service and the known routes — a mis-wired evaluator fails loudly instead of producing a plausible-looking FAIL.

This means one deployed evaluator can back the eval configs of an entire agent fleet: register it once, attach it to each runtime's online eval config, and add one YAML per agent.

## Same contract, different framework

Behavioral contracts validate the process — which tools ran, in what order, against which data sources — so they are independent of the agent framework. To prove it, the sample deploys the claims agent twice: the Strands implementation ([`app/claims_agent/`](claimsdemo/app/claims_agent/)) and a LangGraph implementation ([`app/langgraph_claims_agent/`](claimsdemo/app/langgraph_claims_agent/), `create_react_agent` + `opentelemetry-instrumentation-langchain`). Same tools, same [contract YAML](evaluator/contracts/claims.yaml), zero evaluator configuration changes — the `applies_to: ["*claims_agent*"]` glob matches both runtimes.

Verified live (contract / Helpfulness):

| Scenario | Strands | LangGraph |
|---|---|---|
| Compliant (MEM-001) | PASS 8/8 / 0.83 | PASS 8/8 / 0.83 |
| Lazy (no tools, fabricated citations) | FAIL 4/8 / **1.00** | FAIL 4/8 / 0.83 |
| Fast deny (MEM-003, expired coverage) | PASS 8/8 | PASS 8/8 |

The span shim absorbs the framework differences: LangChain's instrumentation wraps tool results in `ToolMessage` envelopes and puts the final answer on a workflow span — the shim unwraps both so the contract engine sees the same normalized execution either way (covered by `TestLangGraphFramework` in the handler tests, against real captured payloads). The vendored normalization layer also detects and classifies **PydanticAI** and **Claude Agent SDK** traces (tested upstream in the library); deploying demo agents for those is left out to keep the walkthrough small.

## Adapting to your own agent

1. Deploy your agent on AgentCore Runtime with observability enabled (the default).
2. Generate or write a contract YAML (see [Writing contracts](#writing-contracts)) with an `applies_to` pattern matching your runtime's service name.
3. Redeploy the Lambda: `python scripts/deploy_evaluator_lambda.py`. No agent code changes needed.
4. Attach the evaluator to your runtime's eval config (quickstart steps 3 and 6).

To capture your agent's real evaluator payloads as offline test fixtures (the way this sample's fixtures were made), deploy once with `--debug-capture`: each evaluation's raw payload is written to `s3://<audit-bucket>/debug/`. Download, sanitize account identifiers, drop into `tests/fixtures/`, and redeploy without the flag to turn capture off.

## Running the tests

The handler tests run against real captured AgentCore evaluator payloads — no AWS access required. The contract engine is vendored in the repo, so the only installs are the test runner and utility deps:

```bash
python -m venv .venv && source .venv/bin/activate
pip install pytest pyyaml boto3
python -m pytest tests/ -v
```

## Costs

Rough costs for the full walkthrough including the LangGraph variant (~15 agent invocations, us-west-2, July 2026):

| Item | Driver | Estimate |
|---|---|---|
| Amazon Bedrock (Claude Sonnet) | agent invocations + Builtin.Helpfulness judge | < $1 |
| AgentCore Runtime | session-seconds during demo | < $1 |
| Lambda (contract evaluator) | millisecond deterministic checks | < $0.01 |
| CloudWatch | spans, logs, Transaction Search | < $1 |
| S3 | audit records (KB-scale) | < $0.01 |
| **Total** | | **< $5** |

The contract evaluator itself adds no LLM inference cost — that's the point of code-based evaluators.

## Cleanup

```bash
# 1. AgentCore stack (runtime, evaluators, online eval config)
aws cloudformation delete-stack --stack-name AgentCore-claimsdemo-default

# 2. Lambda, IAM role, audit bucket
python scripts/cleanup.py
```

Note: an enabled online evaluation config locks its evaluators. Stack deletion handles the ordering; if deleting manually, disable/delete the online config first.

## Security

- The evaluator Lambda's role has CloudWatch Logs write plus `s3:PutObject` on the audit bucket's `audit/` and `debug/` prefixes only. Debug capture (raw payloads to `debug/`) is off unless deployed with `--debug-capture`.
- The AgentCore Evaluations execution role is granted `lambda:InvokeFunction` on this one function by the CDK stack.
- All demo data (members, drugs, coverage) is synthetic. Audit records may contain agent inputs/outputs — treat the audit bucket according to your data classification if you adapt this to real data.

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for vulnerability reporting.

## License

This sample is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.

The vendored libraries in [`evaluator/vendor/`](evaluator/vendor/README.md) (contract engine and OTEL normalization layer) are licensed under the Apache License 2.0 — see [`THIRD-PARTY-LICENSES`](THIRD-PARTY-LICENSES) and [`evaluator/vendor/LICENSE`](evaluator/vendor/LICENSE).
