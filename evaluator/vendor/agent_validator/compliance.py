# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Compliance tracking: statistical validation over execution windows.

Handles non-deterministic agents by tracking per-constraint compliance rates
over sliding windows and alerting on drift.

The per-execution model is still binary (pass/fail). This module aggregates
those verdicts into compliance rates and detects when rates drop below
acceptable thresholds.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from agent_validator.models import AuditRecord, Verdict, VerdictStatus


@dataclass
class ConstraintCompliance:
    """Compliance rate for a single constraint over a window."""

    name: str
    checked: int = 0
    passed: int = 0

    @property
    def rate(self) -> float:
        return self.passed / self.checked if self.checked > 0 else 1.0

    @property
    def failed(self) -> int:
        return self.checked - self.passed


@dataclass
class CompliancePolicy:
    """Defines acceptable compliance thresholds.

    Args:
        overall_threshold: Minimum overall pass rate (0.0-1.0). Default 0.95 (95%).
        constraint_thresholds: Per-constraint minimum pass rates. Overrides overall.
        min_window_size: Minimum executions before thresholds are enforced.
            Avoids alerting on 1 failure out of 2 executions.
    """

    overall_threshold: float = 0.95
    constraint_thresholds: dict[str, float] = field(default_factory=dict)
    min_window_size: int = 10

    def threshold_for(self, constraint_name: str) -> float:
        return self.constraint_thresholds.get(constraint_name, self.overall_threshold)


@dataclass
class DriftAlert:
    """An alert triggered when compliance drops below threshold."""

    timestamp: datetime
    alert_type: str  # "overall" or "constraint"
    constraint_name: str | None  # None for overall alerts
    current_rate: float
    threshold: float
    window_size: int
    message: str

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "alert_type": self.alert_type,
            "constraint_name": self.constraint_name,
            "current_rate": f"{self.current_rate:.1%}",
            "threshold": f"{self.threshold:.1%}",
            "window_size": self.window_size,
            "message": self.message,
        }


OnDriftAlert = Callable[[DriftAlert], None]


class ComplianceTracker:
    """Tracks compliance rates over a sliding window and detects drift.

    Usage:
        tracker = ComplianceTracker(
            policy=CompliancePolicy(overall_threshold=0.95, min_window_size=10),
            window_size=100,
            on_alert=lambda alert: send_to_slack(alert),
        )

        # Feed verdicts as they arrive
        for verdict in verdicts:
            alerts = tracker.record(verdict)
            # alerts is a list of DriftAlert if any thresholds were breached

        # Check current state
        print(tracker.summary())
    """

    def __init__(
        self,
        policy: CompliancePolicy | None = None,
        window_size: int = 100,
        on_alert: OnDriftAlert | None = None,
    ):
        self.policy = policy or CompliancePolicy()
        self.window_size = window_size
        self.on_alert = on_alert

        # Sliding window of (verdict_status, constraint_results) tuples
        self._window: deque[tuple[VerdictStatus, dict[str, bool]]] = deque(maxlen=window_size)

        # Running counts
        self._total = 0
        self._overall_passed = 0

    def record(self, verdict: Verdict) -> list[DriftAlert]:
        """Record a verdict and return any drift alerts triggered."""
        self._total += 1

        # Track per-constraint results
        constraint_results: dict[str, bool] = {}
        all_constraints_checked = set()

        # Passed constraints
        num_passed = verdict.checks_passed
        num_failed = len(verdict.violations)

        # Build per-constraint pass/fail from violations
        for v in verdict.violations:
            constraint_results[v.constraint_name] = False
            all_constraints_checked.add(v.constraint_name)

        # We don't have individual constraint names for passed ones from the Verdict model,
        # so we track overall pass/fail and per-constraint from violations only
        is_pass = verdict.status == VerdictStatus.PASS
        if is_pass:
            self._overall_passed += 1

        self._window.append((verdict.status, constraint_results))

        # Check thresholds
        return self._check_thresholds()

    def record_audit(self, record: AuditRecord) -> list[DriftAlert]:
        """Record from an AuditRecord (convenience wrapper)."""
        if record.verdict:
            return self.record(record.verdict)
        return []

    def _check_thresholds(self) -> list[DriftAlert]:
        """Check all thresholds against current window and return alerts."""
        alerts: list[DriftAlert] = []
        window_len = len(self._window)

        if window_len < self.policy.min_window_size:
            return alerts  # Not enough data yet

        now = datetime.now(timezone.utc)

        # Overall compliance
        window_passed = sum(1 for status, _ in self._window if status == VerdictStatus.PASS)
        overall_rate = window_passed / window_len

        if overall_rate < self.policy.overall_threshold:
            alert = DriftAlert(
                timestamp=now,
                alert_type="overall",
                constraint_name=None,
                current_rate=overall_rate,
                threshold=self.policy.overall_threshold,
                window_size=window_len,
                message=(
                    f"Overall compliance rate {overall_rate:.1%} dropped below "
                    f"threshold {self.policy.overall_threshold:.1%} "
                    f"(window: {window_len} executions)"
                ),
            )
            alerts.append(alert)
            if self.on_alert:
                self.on_alert(alert)

        # Per-constraint compliance (from violations in the window)
        constraint_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"checked": 0, "failed": 0})
        for _, constraint_results in self._window:
            for constraint_name, passed in constraint_results.items():
                constraint_counts[constraint_name]["checked"] += 1
                if not passed:
                    constraint_counts[constraint_name]["failed"] += 1

        for constraint_name, counts in constraint_counts.items():
            if counts["checked"] < self.policy.min_window_size:
                continue
            fail_rate = counts["failed"] / counts["checked"]
            pass_rate = 1 - fail_rate
            threshold = self.policy.threshold_for(constraint_name)

            if pass_rate < threshold:
                alert = DriftAlert(
                    timestamp=now,
                    alert_type="constraint",
                    constraint_name=constraint_name,
                    current_rate=pass_rate,
                    threshold=threshold,
                    window_size=counts["checked"],
                    message=(
                        f"Constraint '{constraint_name}' compliance {pass_rate:.1%} "
                        f"below threshold {threshold:.1%} "
                        f"({counts['failed']}/{counts['checked']} failures in window)"
                    ),
                )
                alerts.append(alert)
                if self.on_alert:
                    self.on_alert(alert)

        return alerts

    @property
    def overall_compliance(self) -> float:
        """Current overall compliance rate."""
        if not self._window:
            return 1.0
        passed = sum(1 for status, _ in self._window if status == VerdictStatus.PASS)
        return passed / len(self._window)

    @property
    def constraint_compliance(self) -> dict[str, ConstraintCompliance]:
        """Per-constraint compliance rates from the current window."""
        result: dict[str, ConstraintCompliance] = {}
        for _, constraint_results in self._window:
            for name, passed in constraint_results.items():
                if name not in result:
                    result[name] = ConstraintCompliance(name=name)
                result[name].checked += 1
                if passed:
                    result[name].passed += 1
        return result

    def summary(self) -> dict:
        """Current compliance state as a dict."""
        cc = self.constraint_compliance
        failing_constraints = {
            name: f"{c.rate:.1%}" for name, c in cc.items()
            if c.rate < self.policy.threshold_for(name)
        }
        return {
            "window_size": len(self._window),
            "total_recorded": self._total,
            "overall_compliance": f"{self.overall_compliance:.1%}",
            "overall_threshold": f"{self.policy.overall_threshold:.1%}",
            "threshold_met": self.overall_compliance >= self.policy.overall_threshold,
            "failing_constraints": failing_constraints,
            "per_constraint": {
                name: {"rate": f"{c.rate:.1%}", "checked": c.checked, "failed": c.failed}
                for name, c in cc.items()
            },
        }
