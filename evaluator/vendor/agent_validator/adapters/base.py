# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Base class for trace source adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from agent_validator.models import NormalizedExecution


class TraceAdapter(ABC):
    """Abstract base for adapters that normalize trace data into NormalizedExecution."""

    @abstractmethod
    def normalize(self, raw_trace: Any) -> NormalizedExecution:
        """Convert raw trace data into a NormalizedExecution."""
        ...

    def normalize_many(self, raw_traces: list[Any]) -> list[NormalizedExecution]:
        """Normalize a list of raw traces."""
        return [self.normalize(t) for t in raw_traces]
