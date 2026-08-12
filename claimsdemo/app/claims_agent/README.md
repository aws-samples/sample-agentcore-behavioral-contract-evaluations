# claims_agent

The Strands implementation of the demo claims agent: a pharmacy-claims processor with three simulated tools (`check_coverage`, `lookup_formulary`, `render_decision`) over synthetic data, running Claude Sonnet 5 on Bedrock.

The payload's `mode` field selects the behavior per invocation — no redeploy needed:

```json
{"prompt": "Process pharmacy claim ...", "mode": "compliant" | "lazy"}
```

- `compliant` (default): full toolset and a process-following system prompt → the behavioral contract passes
- `lazy`: no tools, answers from parametric knowledge with a plausible fabricated citation → the contract fails with named violations (while LLM output-quality judges still score it well)

A LangGraph implementation of the same agent lives in [`../langgraph_claims_agent/`](../langgraph_claims_agent/) — the same contract validates both. See the [root README](../../../README.md) for the full walkthrough.
