# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Span classification with configurable registry and confidence scores.

R2: Replaces hardcoded TOOL_CALL_OPS/MODEL_CALL_OPS with an extensible registry.
R5: Every classification carries a confidence score (0.0-1.0).
R6: Adds AGENT_INVOCATION as a distinct type.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from agentic_otel.parser import parse_attrs


class SpanType(str, Enum):
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    AGENT_INVOCATION = "agent_invocation"  # R6: sub-agent calls
    RETRIEVAL = "retrieval"
    HUMAN_REVIEW = "human_review"
    EVENT_LOOP = "event_loop"
    CUSTOM = "custom"


class Framework(str, Enum):
    STRANDS = "strands"
    LANGCHAIN = "langchain"
    PYDANTIC_AI = "pydantic_ai"
    CLAUDE_SDK = "claude_sdk"
    UNKNOWN = "unknown"


@dataclass
class Classification:
    """Result of classifying a span."""

    span_type: SpanType
    confidence: float  # 0.0-1.0
    tool_name: str = ""
    model_id: str = ""
    reason: str = ""
    parsed_attrs: dict | None = None  # cached parsed attributes from classify()


class SemanticConventionRegistry:
    """Configurable span classification with framework-specific rules.

    Default rules cover Strands, LangChain, PydanticAI, and Claude Agent SDK.
    Add custom rules via register_tool_op() and register_model_op().
    """

    def __init__(self):
        # Operation names that indicate tool calls
        self.tool_call_ops: set[str] = {"execute_tool"}
        # Operation names that indicate model calls
        self.model_call_ops: set[str] = {"chat", "text_completion", "generate_content", "llm_request"}
        # Operation names that indicate agent invocations
        self.agent_ops: set[str] = {"invoke_agent", "create_agent"}
        # Operation names that indicate event loop cycles
        self.event_loop_ops: set[str] = set()
        # Span name patterns (fallback when gen_ai.operation.name is missing)
        self.tool_name_patterns: list[str] = ["execute_tool", "running tool"]
        self.model_name_patterns: list[str] = ["ChatBedrock.chat", "chat "]

    def register_tool_op(self, op_name: str) -> None:
        self.tool_call_ops.add(op_name)

    def register_model_op(self, op_name: str) -> None:
        self.model_call_ops.add(op_name)

    def classify(self, span: dict) -> Classification:
        """Classify a span by type with confidence score.

        The parsed attributes are cached on the Classification result
        so callers don't need to re-parse them.
        """
        attrs = parse_attrs(span.get("attributes", []))
        op_name = attrs.get("gen_ai.operation.name", "")
        tool_name = attrs.get("gen_ai.tool.name", "")
        model_id = attrs.get("gen_ai.request.model", "")
        span_name = span.get("name", "")

        # Exact match on operation name — highest confidence
        if op_name in self.tool_call_ops or tool_name:
            return Classification(SpanType.TOOL_CALL, 1.0, tool_name=tool_name or span_name,
                                  reason=f"op={op_name}, tool={tool_name}", parsed_attrs=attrs)

        if op_name in self.model_call_ops:
            return Classification(SpanType.MODEL_CALL, 1.0, model_id=model_id,
                                  reason=f"op={op_name}", parsed_attrs=attrs)

        if op_name in self.agent_ops:
            return Classification(SpanType.AGENT_INVOCATION, 1.0,
                                  reason=f"op={op_name}", parsed_attrs=attrs)

        if op_name in self.event_loop_ops or "event_loop" in span_name.lower():
            return Classification(SpanType.EVENT_LOOP, 0.9,
                                  reason="name contains event_loop", parsed_attrs=attrs)

        # Heuristic: check span name patterns — medium confidence
        for pattern in self.tool_name_patterns:
            if pattern in span_name:
                parts = span_name.split(" ", 1)
                inferred_name = parts[1] if len(parts) > 1 else span_name
                return Classification(SpanType.TOOL_CALL, 0.8, tool_name=inferred_name,
                                      reason=f"span name matches '{pattern}'", parsed_attrs=attrs)

        for pattern in self.model_name_patterns:
            if pattern in span_name:
                return Classification(SpanType.MODEL_CALL, 0.8, model_id=model_id,
                                      reason=f"span name matches '{pattern}'", parsed_attrs=attrs)

        # Heuristic: has gen_ai.tool.name but no operation name — medium confidence
        if tool_name:
            return Classification(SpanType.TOOL_CALL, 0.5, tool_name=tool_name,
                                  reason="has gen_ai.tool.name but no operation name", parsed_attrs=attrs)

        # Heuristic: has model attributes but no operation name
        if model_id and any(k.startswith("gen_ai.usage") for k in attrs):
            return Classification(SpanType.MODEL_CALL, 0.5, model_id=model_id,
                                  reason="has model + token attributes but no operation name", parsed_attrs=attrs)

        # Bedrock Knowledge Base
        if "aws.bedrock.knowledge_base.id" in attrs:
            return Classification(SpanType.RETRIEVAL, 0.9,
                                  reason="has bedrock KB attributes", parsed_attrs=attrs)

        # Unknown
        return Classification(SpanType.CUSTOM, 0.0, reason="no matching pattern", parsed_attrs=attrs)

    def detect_framework(self, raw_trace: dict) -> Framework:
        """Detect the agent framework from trace metadata."""
        from agentic_otel.parser import extract_spans, extract_resource_attrs

        resource_attrs = extract_resource_attrs(raw_trace)
        spans = extract_spans(raw_trace)

        # Check resource attributes
        service_name = resource_attrs.get("service.name", "")
        provider = ""
        for span in spans:
            attrs = parse_attrs(span.get("attributes", []))
            provider = attrs.get("gen_ai.provider.name", provider)
            system = attrs.get("gen_ai.system", "")

            if provider == "strands-agents" or system == "strands-agents":
                return Framework.STRANDS
            if "anthropic" in system and "claude" in system.lower():
                return Framework.CLAUDE_SDK

        # Check scope names
        for rs in raw_trace.get("resourceSpans", raw_trace.get("resource_spans", [])):
            for ss in rs.get("scopeSpans", rs.get("scope_spans", [])):
                scope_name = ss.get("scope", {}).get("name", "")
                if "strands" in scope_name:
                    return Framework.STRANDS
                if "langchain" in scope_name or "traceloop" in scope_name:
                    return Framework.LANGCHAIN
                if "pydantic" in scope_name:
                    return Framework.PYDANTIC_AI

        # Check span patterns
        for span in spans:
            name = span.get("name", "")
            if "ChatBedrock" in name:
                return Framework.LANGCHAIN
            if "running tool" in name:
                return Framework.PYDANTIC_AI

        return Framework.UNKNOWN


# Singleton default registry
DEFAULT_REGISTRY = SemanticConventionRegistry()
