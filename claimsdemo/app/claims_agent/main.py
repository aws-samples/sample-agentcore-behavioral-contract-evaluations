"""Claims processing demo agent for AgentCore Runtime.

Payload format:
    {"prompt": "Process pharmacy claim ...", "mode": "compliant" | "lazy"}

Mode selects the system prompt and toolset per invocation (no redeploy needed):
- compliant (default): full toolset, process-following prompt -> contract PASS
- lazy: no tools, answers from parametric knowledge with a plausible
  citation -> contract FAIL (while output-quality evals still score well)
"""

from strands import Agent
from strands.agent.conversation_manager.null_conversation_manager import (
    NullConversationManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from model.load import load_model
from prompts import PROMPTS
from tools import lookup_formulary, check_coverage, render_decision

app = BedrockAgentCoreApp()
log = app.logger

TOOLSETS = {
    "compliant": [lookup_formulary, check_coverage, render_decision],
    "lazy": [],
}


def create_agent(mode: str) -> Agent:
    """Create a fresh agent for the requested compliance mode."""
    return Agent(
        model=load_model(),
        system_prompt=PROMPTS[mode],
        tools=TOOLSETS[mode],
        conversation_manager=NullConversationManager(),
    )


@app.entrypoint
async def invoke(payload, context):
    mode = str(payload.get("mode", "compliant")).lower()
    if mode not in PROMPTS:
        mode = "compliant"
    prompt = payload.get("prompt", "")
    log.info("Invoking claims agent (mode=%s)", mode)

    agent = create_agent(mode)

    async for event in agent.stream_async(prompt):
        if not isinstance(event, dict) or "event" not in event:
            continue
        cbs = event["event"].get("contentBlockStart")
        if cbs is not None and not cbs.get("start"):
            continue
        yield event


if __name__ == "__main__":
    app.run()
