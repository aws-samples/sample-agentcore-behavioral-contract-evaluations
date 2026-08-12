"""LangGraph claims processing demo agent for AgentCore Runtime.

Same behavior and payload format as the Strands agent (app/claims_agent),
implemented with LangGraph's create_react_agent -- the point is that the
SAME behavioral contract validates both, because the contract checks the
process (tools, ordering, sources), not the framework.

Payload format:
    {"prompt": "Process pharmacy claim ...", "mode": "compliant" | "lazy"}

- compliant (default): full toolset, process-following prompt -> contract PASS
- lazy: no tools, answers from parametric knowledge with a plausible
  citation -> contract FAIL (while output-quality evals still score well)
"""

from langchain_core.messages import HumanMessage
from langgraph.prebuilt import create_react_agent
from opentelemetry.instrumentation.langchain import LangchainInstrumentor
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from model.load import load_model
from prompts import PROMPTS
from tools import lookup_formulary, check_coverage, render_decision

LangchainInstrumentor().instrument()

app = BedrockAgentCoreApp()
log = app.logger

_llm = None

TOOLSETS = {
    "compliant": [lookup_formulary, check_coverage, render_decision],
    "lazy": [],
}


def get_or_create_model():
    global _llm
    if _llm is None:
        _llm = load_model()
    return _llm


@app.entrypoint
async def invoke(payload, context):
    mode = str(payload.get("mode", "compliant")).lower()
    if mode not in PROMPTS:
        mode = "compliant"
    prompt = payload.get("prompt", "")
    log.info("Invoking LangGraph claims agent (mode=%s)", mode)

    graph = create_react_agent(
        get_or_create_model(),
        tools=TOOLSETS[mode],
        prompt=PROMPTS[mode],
    )

    result = await graph.ainvoke({"messages": [HumanMessage(content=prompt)]})
    output = result["messages"][-1].content
    return {"result": output}


if __name__ == "__main__":
    app.run()
