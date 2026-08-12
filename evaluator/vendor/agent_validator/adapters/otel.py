# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""OpenTelemetry trace adapter.

Consumes OTEL spans exported as JSON dicts (OTLP JSON export format) and
normalizes them into NormalizedExecution records for contract validation.

Uses agentic_otel for shared parsing (extract_spans, find_root, parse_attrs, etc.).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agentic_otel import (
    DEFAULT_REGISTRY,
    SemanticConventionRegistry,
    SpanType,
    extract_spans,
    find_root,
    parse_attrs,
    parse_timestamp,
    safe_json_parse,
    extract_resource_attrs,
)

from agent_validator.adapters.base import TraceAdapter
from agent_validator.models import DataSourceRef, ExecutionStep, NormalizedExecution, StepType

# Map agentic_otel SpanType to agent_validator StepType
_SPAN_TYPE_TO_STEP_TYPE = {
    SpanType.TOOL_CALL: StepType.TOOL_CALL,
    SpanType.MODEL_CALL: StepType.MODEL_CALL,
    SpanType.RETRIEVAL: StepType.RETRIEVAL,
    SpanType.AGENT_INVOCATION: StepType.CUSTOM,
    SpanType.EVENT_LOOP: StepType.CUSTOM,
    SpanType.HUMAN_REVIEW: StepType.CUSTOM,
    SpanType.CUSTOM: StepType.CUSTOM,
}


class OTELAdapter(TraceAdapter):
    """Normalize OTEL JSON traces into NormalizedExecution records.

    Args:
        tool_source_map: Maps tool names to data source identities.
            Example: {"lookup_formulary": {"source_name": "formulary-db", "source_type": "database"}}
            This is how we bridge "tool X was called" to "data source Y was consulted"
            without requiring custom agent instrumentation.
        registry: Semantic convention registry for span classification.
            Defaults to DEFAULT_REGISTRY. Pass a custom registry to support
            additional frameworks or operation names.
    """

    def __init__(
        self,
        tool_source_map: dict[str, dict[str, str]] | None = None,
        registry: SemanticConventionRegistry | None = None,
    ):
        self.tool_source_map = tool_source_map or {}
        self.registry = registry or DEFAULT_REGISTRY

    def normalize(self, raw_trace: dict) -> NormalizedExecution:
        """Convert an OTLP JSON export dict into a NormalizedExecution."""
        spans = extract_spans(raw_trace)
        if not spans:
            return NormalizedExecution(
                execution_id="unknown",
                trace_source="otel",
                started_at=datetime.now(timezone.utc),
            )

        root_span = find_root(spans)
        child_spans = [s for s in spans if s is not root_span]
        steps = [self._span_to_step(s) for s in child_spans]

        root_attrs = parse_attrs(root_span.get("attributes", []))

        return NormalizedExecution(
            execution_id=root_span.get("traceId", root_span.get("trace_id", "unknown")),
            trace_source="otel",
            started_at=parse_timestamp(root_span.get("startTimeUnixNano", 0)),
            ended_at=parse_timestamp(root_span.get("endTimeUnixNano")),
            steps=steps,
            input_text=self._extract_input(root_span, root_attrs),
            output_text=self._extract_output(spans, root_attrs),
            metadata=extract_resource_attrs(raw_trace),
        )

    # -- Span to step conversion --

    def _span_to_step(self, span: dict) -> ExecutionStep:
        classification = self.registry.classify(span)
        attrs = classification.parsed_attrs or parse_attrs(span.get("attributes", []))
        step_type = _SPAN_TYPE_TO_STEP_TYPE.get(classification.span_type, StepType.CUSTOM)
        tool_name = classification.tool_name or attrs.get("gen_ai.tool.name", span.get("name", "unknown"))

        return ExecutionStep(
            step_id=span.get("spanId", span.get("span_id", "")),
            step_type=step_type,
            name=tool_name,
            started_at=parse_timestamp(span.get("startTimeUnixNano", 0)),
            ended_at=parse_timestamp(span.get("endTimeUnixNano")),
            parent_step_id=span.get("parentSpanId", span.get("parent_span_id")),
            status=self._extract_status(span),
            inputs=safe_json_parse(attrs.get("gen_ai.tool.call.arguments")),
            outputs=safe_json_parse(attrs.get("gen_ai.tool.call.result")),
            data_sources=self._extract_data_sources(attrs, tool_name),
            metadata={
                k: v
                for k, v in {
                    "model": attrs.get("gen_ai.request.model"),
                    "input_tokens": attrs.get("gen_ai.usage.input_tokens"),
                    "output_tokens": attrs.get("gen_ai.usage.output_tokens"),
                    "provider": attrs.get("gen_ai.provider.name"),
                }.items()
                if v is not None
            },
        )

    def _extract_data_sources(self, attrs: dict, tool_name: str) -> list[DataSourceRef]:
        """Infer data source references from span attributes and the tool_source_map."""
        sources: list[DataSourceRef] = []

        # Bedrock Knowledge Base
        kb_id = attrs.get("aws.bedrock.knowledge_base.id")
        if kb_id:
            sources.append(
                DataSourceRef(
                    source_name=kb_id,
                    source_type="knowledge_base",
                    metadata={"bedrock_kb_id": kb_id},
                )
            )

        # Configurable tool -> source mapping
        if tool_name in self.tool_source_map:
            mapping = self.tool_source_map[tool_name]
            sources.append(
                DataSourceRef(
                    source_name=mapping["source_name"],
                    source_type=mapping.get("source_type", "unknown"),
                    version=mapping.get("version"),
                )
            )

        return sources

    # -- Input/output extraction --

    def _extract_input(self, root_span: dict, attrs: dict) -> str | None:
        """Extract the user input from the root span."""
        # Try gen_ai.input.messages from span attributes
        input_msgs = attrs.get("gen_ai.input.messages")
        if input_msgs:
            result = self._parse_messages_for_text(input_msgs, role="user")
            if result:
                return result

        # Strands Agents uses gen_ai.user.message on the root span
        user_msg = attrs.get("gen_ai.user.message")
        if user_msg:
            return str(user_msg)

        # Try span events (Strands with gen_ai_latest_experimental puts content in events)
        result = self._extract_from_events(root_span, "gen_ai.input.messages", role="user")
        if result:
            return result

        return None

    def _extract_output(self, spans: list[dict], root_attrs: dict) -> str | None:
        """Extract the final output from the last model call or root span."""
        root_span = find_root(spans) if spans else {}

        # Try root span's output messages from attributes
        output_msgs = root_attrs.get("gen_ai.output.messages")
        if output_msgs:
            result = self._parse_messages_for_text(output_msgs)
            if result:
                return result

        # Strands Agents uses gen_ai.choice on the root span for the final response
        choice = root_attrs.get("gen_ai.choice")
        if choice:
            result = self._parse_messages_for_text(choice)
            if result:
                return result

        # Try span events on root span (Strands with gen_ai_latest_experimental)
        result = self._extract_from_events(root_span, "gen_ai.output.messages")
        if result:
            return result

        # Fallback: look at the last model_call span's output
        model_spans = [
            s
            for s in spans
            if self.registry.classify(s).span_type == SpanType.MODEL_CALL
        ]
        if model_spans:
            last = model_spans[-1]
            last_attrs = parse_attrs(last.get("attributes", []))
            for attr_name in ("gen_ai.output.messages", "gen_ai.completion.0.content", "gen_ai.choice"):
                val = last_attrs.get(attr_name)
                if val:
                    result = self._parse_messages_for_text(val)
                    if result:
                        return result
            # Try events on last model call span
            result = self._extract_from_events(last, "gen_ai.output.messages")
            if result:
                return result

        return None

    # -- Message parsing helpers --

    def _parse_messages_for_text(self, raw: Any, role: str | None = None) -> str | None:
        """Parse a messages value (string or JSON) and extract text content.

        Handles multiple formats:
        - JSON array: [{"role": "user", "content": "..."}] or [{"role": "user", "parts": [...]}]
        - JSON object: {"role": "assistant", "content": [...]}
        - Plain string
        """
        parsed = safe_json_parse(raw)
        if isinstance(parsed, list):
            msgs = reversed(parsed) if role is None else parsed
            for msg in msgs:
                if not isinstance(msg, dict):
                    continue
                if role and msg.get("role") != role:
                    continue
                # Try "parts" format (Strands events)
                parts = msg.get("parts", [])
                if parts and isinstance(parts, list):
                    text_parts = [
                        p.get("content", "") for p in parts
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    if text_parts:
                        return " ".join(text_parts)
                # Try direct "content" (standard OTEL format)
                content = msg.get("content")
                if isinstance(content, str) and content:
                    return content
                if isinstance(content, list):
                    text_parts = [
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("text")
                    ]
                    if text_parts:
                        return " ".join(text_parts)
        if isinstance(parsed, dict):
            content = parsed.get("content")
            if isinstance(content, str) and content:
                return content
            if isinstance(content, list):
                text_parts = [
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("text")
                ]
                if text_parts:
                    return " ".join(text_parts)
        if isinstance(parsed, str) and parsed:
            return parsed
        return None

    def _extract_from_events(
        self, span: dict, attr_key: str, role: str | None = None
    ) -> str | None:
        """Extract text from span events (OTEL GenAI semantic conventions put content in events)."""
        events = span.get("events", [])
        for event in events:
            event_attrs = parse_attrs(event.get("attributes", []))
            val = event_attrs.get(attr_key)
            if val:
                result = self._parse_messages_for_text(val, role=role)
                if result:
                    return result
        return None

    # -- Utility methods --

    def _extract_status(self, span: dict) -> str:
        status = span.get("status", {})
        code = status.get("code", 0)
        if code == 2:  # ERROR
            return "error"
        return "success"
