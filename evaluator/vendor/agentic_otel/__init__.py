# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Agentic OTEL — normalization layer for OpenTelemetry GenAI agent traces."""

from agentic_otel.parser import (
    extract_spans, find_root, parse_attrs, parse_timestamp,
    safe_json_parse, extract_resource_attrs, sort_spans_by_time,
    get_span_start_ns, get_span_end_ns, spans_overlap,
)
from agentic_otel.classifier import (
    SpanType, Framework, Classification, SemanticConventionRegistry, DEFAULT_REGISTRY,
)
from agentic_otel.normalize import (
    TokenUsage, DataSourceRef, NormalizedSpan, NormalizedTrace, OTELNormalizer,
)

__all__ = [
    "extract_spans", "find_root", "parse_attrs", "parse_timestamp",
    "safe_json_parse", "extract_resource_attrs", "sort_spans_by_time",
    "get_span_start_ns", "get_span_end_ns", "spans_overlap",
    "SpanType", "Framework", "Classification", "SemanticConventionRegistry", "DEFAULT_REGISTRY",
    "TokenUsage", "DataSourceRef", "NormalizedSpan", "NormalizedTrace", "OTELNormalizer",
]
