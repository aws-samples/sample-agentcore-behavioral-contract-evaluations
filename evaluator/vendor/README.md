# Vendored libraries

This directory contains vendored copies of two Apache-2.0 licensed Python
libraries that power the contract evaluator. They are included in-tree so the
sample is fully self-contained: no PyPI packages or external repositories are
required to run the tests or deploy the Lambda.

| Directory | Library | What it does |
|---|---|---|
| `agent_validator/` | agentic-behavioral-contracts | Behavioral contract engine: YAML contract loading, seven deterministic constraint types, conditional constraints (`when:`), validation, audit record generation, and contract auto-generation from known-good traces. |
| `agentic_otel/` | agentic-otel | Zero-dependency normalization layer for OpenTelemetry GenAI agent traces: single-pass parsing, span classification, and framework detection (Strands Agents, LangChain/LangGraph, PydanticAI, Claude Agent SDK). |

## License

Both libraries are licensed under the Apache License 2.0 (see [`LICENSE`](LICENSE)
in this directory). The rest of this sample is licensed under MIT-0; the
Apache-2.0 terms apply only to the code in this directory.

## How the vendoring works

- **Local development and tests:** `evaluator/handler.py` and
  `scripts/generate_contract.py` insert this directory at the front of
  `sys.path`, so `import agent_validator` and `import agentic_otel` resolve
  here without any package installation.
- **Lambda build:** `scripts/deploy_evaluator_lambda.py` copies the package
  directories from here into the root of the deployment zip, so imports
  resolve natively at runtime (the `sys.path` shim becomes a no-op).
- **Runtime dependency:** the only third-party dependency is `pyyaml`
  (required by the contract engine); `agentic_otel` has no dependencies.

## Updating the vendored code

Do not edit files in this directory by hand; changes will be lost on the next
sync. If you maintain the upstream libraries locally,
`scripts/sync_vendor.sh` refreshes this directory from their source trees.
