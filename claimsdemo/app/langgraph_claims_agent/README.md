# langgraph_claims_agent

The LangGraph implementation of the demo claims agent — same tools, prompts, and compliant/lazy modes as the Strands implementation in [`../claims_agent/`](../claims_agent/), built with `langgraph.prebuilt.create_react_agent` and instrumented by `opentelemetry-instrumentation-langchain`.

It exists to demonstrate that the behavioral contract validates the process, not the framework: the identical [contract YAML](../../../evaluator/contracts/claims.yaml) evaluates both implementations with zero configuration changes. See [Same contract, different framework](../../../README.md#same-contract-different-framework) in the root README.

Invoke it with:

```bash
python scripts/invoke_agent.py --runtime langgraph_claims_agent --mode both
```

Payload format (identical to the Strands agent):

```json
{"prompt": "Process pharmacy claim ...", "mode": "compliant" | "lazy"}
```
