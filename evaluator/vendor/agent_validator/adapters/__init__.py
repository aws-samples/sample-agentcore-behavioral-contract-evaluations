# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Trace source adapters."""

from agent_validator.adapters.base import TraceAdapter
from agent_validator.adapters.otel import OTELAdapter

__all__ = ["TraceAdapter", "OTELAdapter"]
