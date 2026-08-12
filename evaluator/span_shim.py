"""Convert AgentCore Evaluations sessionSpans into OTLP-shaped input.

AgentCore code-based evaluators receive `evaluationInput.sessionSpans` --
span records assembled from CloudWatch (verified against captured payloads,
see tests/fixtures/). The records carry both camelCase and snake_case keys,
dict-format attributes, and integer nanosecond timestamps, all of which the
agentic-otel parser handles natively.

Two things the parser does NOT know about:

1. Message content location. AgentCore delivers correlated log events in
   `span_events[]`, each with a `body` dict like
   {"input": {"messages": [...]}, "output": {"messages": [...]}}.
   The adapter expects `gen_ai.input.messages` / `gen_ai.output.messages`
   span attributes. The shim promotes span_events bodies into those
   attributes (the inner message format is already the Strands "parts"
   format the adapter parses).

2. Target filtering. TRACE-level evaluations pass evaluationTarget.traceIds;
   only spans from those traces should be validated.

The shim also normalizes timestamps defensively for span records that carry
only ISO-8601 or epoch-millisecond timestamps.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

# Candidate keys for start/end timestamps across span record formats.
_START_KEYS = ("startTimeUnixNano", "start_time_unix_nano", "startTime", "start_time")
_END_KEYS = ("endTimeUnixNano", "end_time_unix_nano", "endTime", "end_time")


def _to_unix_nano(value: Any) -> int | None:
    """Best-effort conversion of a timestamp value to unix nanoseconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        # Heuristic on magnitude: ns > 1e17, us > 1e14, ms > 1e11, else seconds.
        if v > 1e17:
            return int(v)
        if v > 1e14:
            return int(v * 1e3)
        if v > 1e11:
            return int(v * 1e6)
        return int(v * 1e9)
    if isinstance(value, str):
        try:
            return _to_unix_nano(float(value))
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1e9)
        except ValueError:
            return None
    return None


def _get_trace_id(span: dict) -> str:
    return str(span.get("traceId", span.get("trace_id", "")))


def extract_service_names(session_spans: list[dict]) -> set[str]:
    """Collect the service identities present in a span set.

    AgentCore stamps every span with the runtime's service name (e.g.
    "claimsdemo_claims_agent.DEFAULT") in two places: the OTEL resource
    attributes ("service.name") and the span attributes ("aws.local.service").
    Contract routing matches against these.
    """
    names: set[str] = set()
    for span in session_spans or []:
        if not isinstance(span, dict):
            continue
        resource_attrs = (span.get("resource") or {}).get("attributes") or {}
        if isinstance(resource_attrs, dict):
            svc = resource_attrs.get("service.name")
            if svc:
                names.add(str(svc))
        attrs = span.get("attributes") or {}
        if isinstance(attrs, dict):
            svc = attrs.get("aws.local.service")
            if svc:
                names.add(str(svc))
    return names


def _text_from_blocks(blocks: Any) -> str:
    """Extract text from a list of content blocks in any observed format.

    Handles:
    - Strands parts messages:   [{"role": ..., "parts": [{"type": "text", "content": ...}]}]
    - Bedrock content blocks:   [{"text": "..."}, {"toolUse": {...}}]
    - Plain dicts with "content" strings
    """
    if isinstance(blocks, str):
        return blocks
    texts: list[str] = []
    if isinstance(blocks, list):
        for item in blocks:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("text"), str):  # Bedrock block
                texts.append(item["text"])
            for part in item.get("parts") or []:  # Strands parts message
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(str(part.get("content", "")))
            if isinstance(item.get("content"), str) and "parts" not in item and "text" not in item:
                texts.append(item["content"])
    return " ".join(t for t in texts if t)


def _decode_message_text(msg: dict) -> str:
    """Extract plain text from one span_events message entry.

    Observed variants for msg["content"]:
    - str: JSON-encoded list of Strands parts messages (experimental semconv)
    - dict with "message" or "content": plain text or JSON-encoded Bedrock
      content blocks (default AgentCore instrumentation)
    """
    content = msg.get("content")
    raw: Any = None
    if isinstance(content, str):
        raw = content
    elif isinstance(content, dict):
        raw = content.get("message", content.get("content"))

    if raw is None:
        return ""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw  # plain text
        return _text_from_blocks(parsed)
    return _text_from_blocks(raw)


def _promote_span_event_messages(span: dict, attrs: dict) -> None:
    """Promote message content from span_events bodies into gen_ai.* attributes.

    AgentCore correlates log events to spans as `span_events`, each with a
    `body` that may contain {"input": {"messages": [...]}} and/or
    {"output": {"messages": [...]}}. The inner content encoding differs
    between default and experimental instrumentation; _decode_message_text
    handles both. We map the decoded text to the attributes the OTEL
    adapter already understands:

      input.messages  -> gen_ai.input.messages   [{"role": ..., "content": text}]
      output.messages -> gen_ai.output.messages  [{"role": ..., "content": text}]

    For TOOL spans (gen_ai.tool.name present), the decoded input/output are
    additionally promoted to gen_ai.tool.call.arguments / .result so the
    adapter exposes them as step inputs/outputs -- which is what
    tool_result_matches contract conditions inspect.
    """
    is_tool_span = bool(attrs.get("gen_ai.tool.name"))
    for ev in span.get("span_events") or []:
        if not isinstance(ev, dict):
            continue
        body = ev.get("body")
        if not isinstance(body, dict):
            continue
        for src_key, attr_key, tool_attr_key in (
            ("input", "gen_ai.input.messages", "gen_ai.tool.call.arguments"),
            ("output", "gen_ai.output.messages", "gen_ai.tool.call.result"),
        ):
            block = body.get(src_key)
            if not isinstance(block, dict):
                continue
            messages = block.get("messages")
            if not messages:
                continue
            decoded = []
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                text = _decode_message_text(msg)
                if text:
                    decoded.append({"role": msg.get("role", "assistant"), "content": text})
            if decoded:
                # Later span_events overwrite earlier ones: the final event on
                # a span carries the final message state.
                attrs[attr_key] = json.dumps(decoded)
                if is_tool_span:
                    # The last message's text is the tool's serialized
                    # arguments (input) or result (output); the adapter
                    # safe_json_parses it into step inputs/outputs.
                    attrs[tool_attr_key] = decoded[-1]["content"]


def _unwrap_langchain_tool_result(raw: str) -> str:
    """Unwrap the LangChain ToolMessage envelope to the tool's own output.

    opentelemetry-instrumentation-langchain (the instrumentation the AgentCore
    LangChain/LangGraph template uses) serializes tool results as a serialized
    constructor envelope:

        {"output": {"lc": 1, "type": "constructor",
                    "id": [..., "ToolMessage"],
                    "kwargs": {"content": "<the tool's actual return value>", ...}},
         "kwargs": {...}}

    tool_result_matches contract conditions need the inner content (e.g.
    {"coverage_active": true, ...}). Payloads that don't match this shape --
    Strands results, plain JSON, plain text -- are returned unchanged.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return raw
    if not isinstance(data, dict):
        return raw
    node = data.get("output", data)
    if isinstance(node, dict) and node.get("type") == "constructor":
        kwargs = node.get("kwargs")
        if isinstance(kwargs, dict) and isinstance(kwargs.get("content"), str):
            return kwargs["content"]
    return raw


def _unwrap_langchain_tool_arguments(raw: str) -> str:
    """Unwrap LangChain's tool-input envelope to the call arguments.

    The instrumentation wraps arguments as {"input_str": "<repr of kwargs>",
    "tags": [...], "metadata": {...}}. The repr is a Python literal, not JSON;
    convert it so the adapter can parse arguments into a dict. Anything that
    doesn't match is returned unchanged.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return raw
    if isinstance(data, dict) and isinstance(data.get("input_str"), str):
        import ast

        try:
            literal = ast.literal_eval(data["input_str"])
            return json.dumps(literal)
        except (ValueError, SyntaxError):
            return data["input_str"]
    return raw


def _decode_langchain_workflow_output(attrs: dict) -> str:
    """Final response text from a LangChain workflow span's output envelope.

    The LangGraph root span (gen_ai.operation.name=invoke_agent) carries the
    conversation state as {"outputs": {"messages": [<lc constructor>, ...]}}
    in gen_ai.task.output / traceloop.entity.output. The final answer is the
    last AIMessage's kwargs.content (a string, or Bedrock content blocks).
    """
    raw = attrs.get("gen_ai.task.output") or attrs.get("traceloop.entity.output")
    if not isinstance(raw, str):
        return ""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ""
    outputs = data.get("outputs") if isinstance(data, dict) else None
    if isinstance(outputs, dict):
        messages = outputs.get("messages")
    elif isinstance(outputs, list):
        messages = outputs
    else:
        return ""
    if not isinstance(messages, list):
        return ""
    text = ""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        ident = msg.get("id")
        if not (isinstance(ident, list) and ident and ident[-1] == "AIMessage"):
            continue
        content = (msg.get("kwargs") or {}).get("content")
        if isinstance(content, str) and content:
            text = content  # keep the LAST AIMessage: the final answer
        elif isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            joined = " ".join(p for p in parts if p)
            if joined:
                text = joined
    return text


def _normalize_span(span: dict) -> dict:
    """Return a copy of the span with nanosecond timestamps and promoted content."""
    out = dict(span)
    if not out.get("startTimeUnixNano"):
        for key in _START_KEYS:
            ns = _to_unix_nano(span.get(key))
            if ns:
                out["startTimeUnixNano"] = ns
                break
    if not out.get("endTimeUnixNano"):
        for key in _END_KEYS:
            ns = _to_unix_nano(span.get(key))
            if ns:
                out["endTimeUnixNano"] = ns
                break

    attrs = out.get("attributes")
    if isinstance(attrs, dict):
        attrs = dict(attrs)
    elif isinstance(attrs, list):
        # OTLP KV-list format: flatten to a dict (values may stay wrapped;
        # agentic-otel's parse_attrs also accepts already-parsed dicts).
        flat: dict[str, Any] = {}
        for item in attrs:
            if isinstance(item, dict) and "key" in item:
                value = item.get("value")
                if isinstance(value, dict) and len(value) == 1:
                    value = next(iter(value.values()))
                flat[item["key"]] = value
        attrs = flat
    else:
        attrs = {}
    _promote_span_event_messages(span, attrs)
    if (attrs.get("gen_ai.operation.name") == "invoke_agent"
            and "gen_ai.output.messages" not in attrs):
        # LangChain/LangGraph workflow root: promote the final AIMessage so
        # the adapter extracts output_text (used by must_contain_citations).
        final_text = _decode_langchain_workflow_output(attrs)
        if final_text:
            attrs["gen_ai.output.messages"] = json.dumps(
                [{"role": "assistant", "content": final_text}]
            )
    if attrs.get("gen_ai.tool.name"):
        # Framework envelopes around tool payloads (LangChain ToolMessage,
        # traceloop input_str) are unwrapped so contract conditions see the
        # tool's own values regardless of the agent framework.
        for key, unwrap in (
            ("gen_ai.tool.call.result", _unwrap_langchain_tool_result),
            ("gen_ai.tool.call.arguments", _unwrap_langchain_tool_arguments),
        ):
            value = attrs.get(key)
            if isinstance(value, str):
                attrs[key] = unwrap(value)
    out["attributes"] = attrs
    return out


def _is_infra_span(span: dict) -> bool:
    """True for HTTP server plumbing spans (e.g. "POST /invocations").

    These carry no agent behavior. Dropping them makes the agent invocation
    span the trace root, which is where the final response text lives.
    """
    attrs = span.get("attributes") or {}
    if isinstance(attrs, dict):
        return "http.method" in attrs or "http.route" in attrs
    return False


def shim_to_otlp(session_spans: list[dict], target_trace_ids: set[str] | None = None) -> dict:
    """Convert sessionSpans to a flat OTLP-style dict for OTELAdapter.

    Args:
        session_spans: The raw span records from evaluationInput.sessionSpans.
        target_trace_ids: When provided (TRACE-level evaluation), only spans
            belonging to these traces are included.

    Returns:
        {"spans": [...]} sorted by start time -- consumable by
        agentic_otel.extract_spans. Step-ordering constraints (must_precede)
        rely on this sort; CloudWatch result order is not chronological.
    """
    spans = []
    for span in session_spans or []:
        if not isinstance(span, dict):
            continue
        if target_trace_ids and _get_trace_id(span) not in target_trace_ids:
            continue
        if _is_infra_span(span):
            continue
        spans.append(_normalize_span(span))
    spans.sort(key=lambda s: int(s.get("startTimeUnixNano", 0) or 0))
    _ensure_root_has_output(spans)
    return {"spans": spans}


def _ensure_root_has_output(spans: list[dict]) -> None:
    """Copy the final output onto the span the adapter treats as the root.

    The adapter extracts output_text from the first parentless span. In
    LangChain/LangGraph traces that is an instrumentation wrapper span with
    no message content -- the final answer lives on the LangGraph.workflow
    span (promoted by _normalize_span). Mirror it onto the root so
    must_contain_citations sees the output regardless of framework.
    """
    if not spans:
        return
    root = None
    for span in spans:
        parent = span.get("parentSpanId", span.get("parent_span_id", ""))
        if not parent or parent == "0" * 16:
            root = span
            break
    if root is None:
        # No parentless span (the parent is often a dropped infra span):
        # the adapter falls back to the earliest span; mirror that.
        root = spans[0]
    root_attrs = root.get("attributes") or {}
    if root_attrs.get("gen_ai.output.messages"):
        return
    for span in reversed(spans):  # last invocation output wins
        attrs = span.get("attributes") or {}
        if (attrs.get("gen_ai.operation.name") == "invoke_agent"
                and attrs.get("gen_ai.output.messages")):
            root_attrs["gen_ai.output.messages"] = attrs["gen_ai.output.messages"]
            root["attributes"] = root_attrs
            return
