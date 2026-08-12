# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The single normalization pass — raw OTEL JSON to NormalizedTrace.

Downstream analysis tools consume NormalizedTrace instead of raw dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agentic_otel.classifier import (
    Classification,
    DEFAULT_REGISTRY,
    Framework,
    SemanticConventionRegistry,
    SpanType,
)
from agentic_otel.parser import (
    extract_resource_attrs,
    extract_spans,
    find_root,
    get_span_start_ns,
    parse_attrs,
    parse_timestamp,
    safe_json_parse,
    sort_spans_by_time,
)


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0


@dataclass
class DataSourceRef:
    source_name: str
    source_type: str
    version: str | None = None


@dataclass
class NormalizedSpan:
    """A single span, fully parsed and classified."""

    span_id: str
    parent_span_id: str | None
    span_type: SpanType
    classification_confidence: float
    name: str
    start_time: datetime
    end_time: datetime | None = None
    status: str = "success"
    attributes: dict = field(default_factory=dict)
    inputs: Any = None
    outputs: Any = None
    token_usage: TokenUsage | None = None
    data_sources: list[DataSourceRef] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    raw_span: dict = field(default_factory=dict, repr=False)


@dataclass
class NormalizedTrace:
    """The canonical representation of an agent execution.

    Produced once by the normalizer, consumed by downstream analysis tools.
    """

    execution_id: str
    framework: Framework
    root_span: NormalizedSpan | None = None
    spans: list[NormalizedSpan] = field(default_factory=list)
    span_tree: dict[str, list[str]] = field(default_factory=dict)
    input_text: str | None = None
    output_text: str | None = None
    resource_attrs: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    validation_errors: list[str] = field(default_factory=list)

    # Convenience accessors
    def tool_calls(self) -> list[NormalizedSpan]:
        return [s for s in self.spans if s.span_type == SpanType.TOOL_CALL]

    def model_calls(self) -> list[NormalizedSpan]:
        return [s for s in self.spans if s.span_type == SpanType.MODEL_CALL]

    def agent_invocations(self) -> list[NormalizedSpan]:
        return [s for s in self.spans if s.span_type == SpanType.AGENT_INVOCATION]

    def step_names(self) -> list[str]:
        """Names of tool calls, model calls, and agent invocations (excludes EVENT_LOOP/CUSTOM)."""
        return [s.name for s in self.spans
                if s.span_type not in (SpanType.EVENT_LOOP, SpanType.CUSTOM)]

    def data_source_names(self) -> set[str]:
        names: set[str] = set()
        for span in self.spans:
            for ds in span.data_sources:
                names.add(ds.source_name)
        return names

    def total_tokens(self) -> TokenUsage:
        total = TokenUsage()
        for span in self.spans:
            if span.token_usage:
                total.input_tokens += span.token_usage.input_tokens
                total.output_tokens += span.token_usage.output_tokens
                total.cache_read_tokens += span.token_usage.cache_read_tokens
                total.cache_write_tokens += span.token_usage.cache_write_tokens
                total.total_tokens += span.token_usage.total_tokens
        return total


class OTELNormalizer:
    """The single normalization pass.

    Converts raw OTEL JSON into a NormalizedTrace for downstream analysis.
    """

    def __init__(
        self,
        tool_source_map: dict[str, dict[str, str]] | None = None,
        registry: SemanticConventionRegistry | None = None,
    ):
        self.tool_source_map = tool_source_map or {}
        self.registry = registry or DEFAULT_REGISTRY

    def normalize(self, raw_trace: dict) -> NormalizedTrace:
        """Normalize a raw OTEL trace into the canonical representation."""
        raw_spans = extract_spans(raw_trace)
        if not raw_spans:
            return NormalizedTrace(
                execution_id="unknown",
                framework=Framework.UNKNOWN,
                validation_errors=["No spans found in trace"],
            )

        # Detect framework
        framework = self.registry.detect_framework(raw_trace)

        # Find root
        root_raw = find_root(raw_spans)
        root_attrs = parse_attrs(root_raw.get("attributes", []))
        exec_id = root_raw.get("traceId", root_raw.get("trace_id", "unknown"))

        # Normalize root span
        root_classification = self.registry.classify(root_raw)
        root_span = self._normalize_span(root_raw, root_classification)

        # Sort child spans by timestamp (R3)
        child_raws = [s for s in raw_spans if s is not root_raw]
        child_raws = sort_spans_by_time(child_raws)

        # Normalize all child spans
        spans: list[NormalizedSpan] = []
        span_tree: dict[str, list[str]] = {}

        for raw in child_raws:
            classification = self.registry.classify(raw)
            span = self._normalize_span(raw, classification)
            spans.append(span)

            # Build tree
            if span.parent_span_id:
                span_tree.setdefault(span.parent_span_id, []).append(span.span_id)

        # Extract input/output text
        input_text = self._extract_input(root_raw, root_attrs, raw_spans)
        output_text = self._extract_output(root_raw, root_attrs, raw_spans)

        # Resource attributes
        resource_attrs = extract_resource_attrs(raw_trace)

        trace = NormalizedTrace(
            execution_id=exec_id,
            framework=framework,
            root_span=root_span,
            spans=spans,
            span_tree=span_tree,
            input_text=input_text,
            output_text=output_text,
            resource_attrs=resource_attrs,
        )

        # R4: Validate
        trace.validation_errors = self._validate(trace)

        return trace

    def _normalize_span(self, raw: dict, classification: Classification) -> NormalizedSpan:
        attrs = parse_attrs(raw.get("attributes", []))
        tool_name = classification.tool_name or attrs.get("gen_ai.tool.name", raw.get("name", ""))

        # Token usage
        token_usage = None
        input_t = int(attrs.get("gen_ai.usage.input_tokens", 0) or 0)
        output_t = int(attrs.get("gen_ai.usage.output_tokens", 0) or 0)
        if input_t or output_t:
            token_usage = TokenUsage(
                input_tokens=input_t,
                output_tokens=output_t,
                cache_read_tokens=int(attrs.get("gen_ai.usage.cache_read_input_tokens", 0) or 0),
                cache_write_tokens=int(attrs.get("gen_ai.usage.cache_write_input_tokens", 0) or 0),
                total_tokens=int(attrs.get("gen_ai.usage.total_tokens", 0) or 0),
            )

        # Data sources from tool_source_map
        data_sources = []
        if tool_name in self.tool_source_map:
            mapping = self.tool_source_map[tool_name]
            data_sources.append(DataSourceRef(
                source_name=mapping["source_name"],
                source_type=mapping.get("source_type", "unknown"),
                version=mapping.get("version"),
            ))

        # Bedrock KB
        kb_id = attrs.get("aws.bedrock.knowledge_base.id")
        if kb_id:
            data_sources.append(DataSourceRef(source_name=kb_id, source_type="knowledge_base"))

        # Parse events
        events = []
        for event in raw.get("events", []):
            event_attrs = parse_attrs(event.get("attributes", []))
            events.append({"name": event.get("name", ""), "attributes": event_attrs,
                          "timestamp": event.get("timeUnixNano")})

        # Inputs/outputs
        inputs = safe_json_parse(attrs.get("gen_ai.tool.call.arguments"))
        outputs = safe_json_parse(attrs.get("gen_ai.tool.call.result"))

        # Status
        status_raw = raw.get("status", {})
        status = "error" if status_raw.get("code") == 2 else "success"

        return NormalizedSpan(
            span_id=raw.get("spanId", raw.get("span_id", "")),
            parent_span_id=raw.get("parentSpanId", raw.get("parent_span_id")) or None,
            span_type=classification.span_type,
            classification_confidence=classification.confidence,
            name=tool_name if classification.span_type == SpanType.TOOL_CALL else
                 (classification.model_id or raw.get("name", "unknown")),
            start_time=parse_timestamp(raw.get("startTimeUnixNano", raw.get("start_time_unix_nano"))),
            end_time=parse_timestamp(raw.get("endTimeUnixNano", raw.get("end_time_unix_nano"))),
            status=status,
            attributes=attrs,
            inputs=inputs,
            outputs=outputs,
            token_usage=token_usage,
            data_sources=data_sources,
            events=events,
            raw_span=raw,
        )

    def _extract_input(self, root: dict, attrs: dict, spans: list[dict]) -> str | None:
        for key in ("gen_ai.input.messages", "gen_ai.user.message"):
            val = attrs.get(key)
            if val:
                return self._parse_message_text(val, role="user")
        for event in root.get("events", []):
            ea = parse_attrs(event.get("attributes", []))
            val = ea.get("gen_ai.input.messages")
            if val:
                return self._parse_message_text(val, role="user")
        return None

    def _extract_output(self, root: dict, attrs: dict, spans: list[dict]) -> str | None:
        for key in ("gen_ai.output.messages", "gen_ai.choice"):
            val = attrs.get(key)
            if val:
                return self._parse_message_text(val)
        for event in root.get("events", []):
            ea = parse_attrs(event.get("attributes", []))
            val = ea.get("gen_ai.output.messages")
            if val:
                return self._parse_message_text(val)
        # Fallback: last model call span
        model_spans = [s for s in spans if parse_attrs(s.get("attributes", [])).get("gen_ai.operation.name") in self.registry.model_call_ops]
        if model_spans:
            last_attrs = parse_attrs(model_spans[-1].get("attributes", []))
            for key in ("gen_ai.output.messages", "gen_ai.choice"):
                val = last_attrs.get(key)
                if val:
                    return self._parse_message_text(val)
        return None

    def _parse_message_text(self, raw: Any, role: str | None = None) -> str | None:
        parsed = safe_json_parse(raw)
        if isinstance(parsed, list):
            for msg in (reversed(parsed) if role is None else parsed):
                if not isinstance(msg, dict):
                    continue
                if role and msg.get("role") != role:
                    continue
                parts = msg.get("parts", [])
                if parts:
                    texts = [p.get("content", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
                    if texts:
                        return " ".join(texts)
                content = msg.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("text")]
                    if texts:
                        return " ".join(texts)
        if isinstance(parsed, dict):
            content = parsed.get("content")
            if isinstance(content, str):
                return content
        if isinstance(parsed, str):
            return parsed
        return None

    # R4: Validation
    def _validate(self, trace: NormalizedTrace) -> list[str]:
        errors = []
        if trace.execution_id == "unknown":
            errors.append("execution_id is 'unknown' — trace may be malformed")
        if not trace.spans:
            errors.append("No child spans found — trace may be empty")
        # Check for low-confidence classifications
        low_conf = [s for s in trace.spans if s.classification_confidence < 0.5 and s.span_type != SpanType.EVENT_LOOP]
        if low_conf:
            errors.append(f"{len(low_conf)} spans with low classification confidence (<0.5)")
        # Check timestamp consistency
        for span in trace.spans:
            if span.end_time and span.start_time and span.end_time < span.start_time:
                errors.append(f"Span {span.span_id}: end_time before start_time")
        return errors
