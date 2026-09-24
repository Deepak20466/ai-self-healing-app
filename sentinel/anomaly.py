"""In-memory anomaly detection: 5xx rate and p95-latency-vs-baseline.

SPEC.md: "5xx rate > 5% or p95 latency > 2x baseline over 5 minutes. These
are reported to the chat and do not auto-fix." The detector itself is pure
(no I/O, no clock dependency beyond an injected `now`) so it's cheap to unit
test without waiting on real time; `run_anomaly_loop` is the thin async
wrapper that actually schedules it and persists findings via storage.py.
"""

from __future__ import annotations

import asyncio
import bisect
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from core.config import settings
from core.db import async_session_factory
from sentinel import storage


@dataclass
class _Sample:
    occurred_at: datetime
    status_code: int
    duration_ms: float


@dataclass
class AnomalyFinding:
    type: str
    metric_value: float
    threshold: float
    window_start: datetime
    window_end: datetime


@dataclass
class AnomalyDetector:
    """A rolling buffer of request outcomes plus pure threshold evaluation."""

    window_seconds: int = settings.anomaly_window_seconds
    baseline_window_seconds: int = settings.anomaly_baseline_window_seconds
    min_requests_in_window: int = settings.anomaly_min_requests_in_window
    error_rate_threshold: float = settings.anomaly_error_rate_threshold
    latency_multiplier_threshold: float = settings.anomaly_latency_multiplier_threshold

    _samples: list[_Sample] = field(default_factory=list)

    def record_request(
        self, *, status_code: int, duration_ms: float, occurred_at: datetime
    ) -> None:
        # `_samples` is kept sorted by `occurred_at` because callers report in
        # near-real-time order; a plain append is amortized O(1) for that case.
        self._samples.append(
            _Sample(occurred_at=occurred_at, status_code=status_code, duration_ms=duration_ms)
        )

    def _prune_older_than(self, cutoff: datetime) -> None:
        occurred_ats = [s.occurred_at for s in self._samples]
        keep_from = bisect.bisect_left(occurred_ats, cutoff)
        del self._samples[:keep_from]

    def evaluate(self, now: datetime) -> list[AnomalyFinding]:
        """Pure evaluation: compare the last `window_seconds` against baseline."""
        self._prune_older_than(now - timedelta(seconds=self.baseline_window_seconds))

        window_start = now - timedelta(seconds=self.window_seconds)
        recent = [s for s in self._samples if s.occurred_at >= window_start]
        baseline = [s for s in self._samples if s.occurred_at < window_start]

        findings: list[AnomalyFinding] = []
        if len(recent) < self.min_requests_in_window:
            return findings

        error_count = sum(1 for s in recent if s.status_code >= 500)
        error_rate = error_count / len(recent)
        if error_rate > self.error_rate_threshold:
            findings.append(
                AnomalyFinding(
                    type="5xx_rate",
                    metric_value=round(error_rate, 4),
                    threshold=self.error_rate_threshold,
                    window_start=window_start,
                    window_end=now,
                )
            )

        if len(baseline) >= self.min_requests_in_window:
            recent_p95 = _percentile([s.duration_ms for s in recent], 0.95)
            baseline_p95 = _percentile([s.duration_ms for s in baseline], 0.95)
            if baseline_p95 > 0 and recent_p95 > baseline_p95 * self.latency_multiplier_threshold:
                findings.append(
                    AnomalyFinding(
                        type="latency_p95",
                        metric_value=round(recent_p95, 2),
                        threshold=round(baseline_p95 * self.latency_multiplier_threshold, 2),
                        window_start=window_start,
                        window_end=now,
                    )
                )

        return findings


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


async def run_anomaly_loop(
    detector: AnomalyDetector, *, interval_seconds: int | None = None
) -> AsyncIterator[list[AnomalyFinding]]:
    """Background loop: evaluate on an interval and persist any findings.

    Yields the findings from each tick (useful for tests); real callers just
    iterate it forever as a fire-and-forget asyncio task.
    """
    interval = interval_seconds or settings.anomaly_eval_interval_seconds
    while True:
        await asyncio.sleep(interval)
        findings = detector.evaluate(datetime.now(UTC))
        if findings:
            async with async_session_factory() as session:
                for finding in findings:
                    await storage.record_anomaly(
                        session,
                        anomaly_type=finding.type,
                        metric_value=finding.metric_value,
                        threshold=finding.threshold,
                        window_start=finding.window_start,
                        window_end=finding.window_end,
                    )
        yield findings
