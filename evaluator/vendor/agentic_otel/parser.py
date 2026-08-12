# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Shared OTEL trace parsing — the single normalization pass.

Trace-analysis tools use this instead of duplicating parsing logic.
Handles: OTLP JSON extraction, root span finding, attribute parsing,
span classification, framework detection, and timestamp normalization.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def extract_spans(raw_trace: dict) -> list[dict]:
    """Extract a flat list of span dicts from any OTLP JSON format.

    Handles:
    - Nested: resourceSpans > scopeSpans > spans (camelCase)
    - Nested: resource_spans > scope_spans > spans (snake_case)
    - Flat: {"spans": [...]}
    """
    spans: list[dict] = []
    for rs in raw_trace.get("resourceSpans", raw_trace.get("resource_spans", [])):
        for ss in rs.get("scopeSpans", rs.get("scope_spans", [])):
            spans.extend(ss.get("spans", []))
    if not spans and isinstance(raw_trace.get("spans"), list):
        spans = raw_trace["spans"]
    return spans


def find_root(spans: list[dict]) -> dict:
    """Find the root span (no parent or empty parent).

    Falls back to the span with the earliest start time.
    """
    for span in spans:
        parent = span.get("parentSpanId", span.get("parent_span_id", ""))
        if not parent or parent == "" or parent == "0" * 16:
            return span
    if not spans:
        return {}
    return min(spans, key=lambda s: int(s.get("startTimeUnixNano", s.get("start_time_unix_nano", 0)) or 0))


def parse_attrs(attributes: list[dict] | dict) -> dict[str, Any]:
    """Convert OTLP attribute array or dict to a plain dict.

    Handles:
    - List format: [{"key": "k", "value": {"stringValue": "v"}}]
    - Dict format: {"k": "v"} (already parsed, e.g., from CloudWatch)
    """
    if isinstance(attributes, dict):
        return attributes
    result: dict[str, Any] = {}
    for attr in attributes:
        key = attr.get("key", "")
        value = attr.get("value", {})
        if isinstance(value, dict):
            for vtype in ("stringValue", "intValue", "boolValue", "doubleValue"):
                if vtype in value:
                    result[key] = value[vtype]
                    break
            else:
                # Check arrayValue
                if "arrayValue" in value:
                    result[key] = value["arrayValue"]
                else:
                    result[key] = value
        else:
            result[key] = value
    return result


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_timestamp(nano_timestamp: int | str | None) -> datetime:
    """Convert nanosecond Unix timestamp to datetime.

    Returns epoch (1970-01-01) if timestamp is missing/invalid,
    which is detectable (unlike returning now()).
    """
    if not nano_timestamp:
        return _EPOCH
    try:
        ts = int(nano_timestamp)
        return datetime.fromtimestamp(ts / 1_000_000_000, tz=timezone.utc)
    except (ValueError, OSError):
        return _EPOCH


def safe_json_parse(value: Any) -> Any:
    """Try to parse a string as JSON, return as-is if not parseable."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


def extract_resource_attrs(raw_trace: dict) -> dict:
    """Extract resource-level attributes (service name, etc.).

    Merges attributes from all resourceSpans entries (multi-service traces).
    """
    result: dict = {}
    for rs in raw_trace.get("resourceSpans", raw_trace.get("resource_spans", [])):
        resource = rs.get("resource", {})
        result.update(parse_attrs(resource.get("attributes", [])))
    return result


def get_span_start_ns(span: dict) -> int:
    """Get span start time as nanoseconds (int), for sorting."""
    return int(span.get("startTimeUnixNano", span.get("start_time_unix_nano", 0)) or 0)


def get_span_end_ns(span: dict) -> int:
    """Get span end time as nanoseconds (int)."""
    return int(span.get("endTimeUnixNano", span.get("end_time_unix_nano", 0)) or 0)


def sort_spans_by_time(spans: list[dict]) -> list[dict]:
    """Sort spans by start time (nanoseconds)."""
    return sorted(spans, key=get_span_start_ns)


def spans_overlap(span_a: dict, span_b: dict) -> bool:
    """Check if two spans have overlapping time ranges (parallel execution)."""
    a_start = get_span_start_ns(span_a)
    a_end = get_span_end_ns(span_a)
    b_start = get_span_start_ns(span_b)
    b_end = get_span_end_ns(span_b)
    if a_end == 0 or b_end == 0:
        return False  # can't determine overlap without end times
    return a_start < b_end and b_start < a_end
