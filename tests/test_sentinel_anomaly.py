"""Unit tests for sentinel.anomaly.AnomalyDetector: pure, no real time needed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sentinel.anomaly import AnomalyDetector

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _detector() -> AnomalyDetector:
    return AnomalyDetector(
        window_seconds=300,
        baseline_window_seconds=3600,
        min_requests_in_window=20,
        error_rate_threshold=0.05,
        latency_multiplier_threshold=2.0,
    )


def _feed(
    detector: AnomalyDetector, count: int, *, status_code: int, duration_ms: float, ago_seconds: int
) -> None:
    for i in range(count):
        detector.record_request(
            status_code=status_code,
            duration_ms=duration_ms,
            occurred_at=NOW - timedelta(seconds=ago_seconds, milliseconds=i),
        )


def test_too_few_requests_in_window_produces_no_findings() -> None:
    detector = _detector()
    _feed(detector, 5, status_code=500, duration_ms=100, ago_seconds=10)

    assert detector.evaluate(NOW) == []


def test_healthy_traffic_produces_no_findings() -> None:
    detector = _detector()
    _feed(detector, 100, status_code=200, duration_ms=50, ago_seconds=10)

    assert detector.evaluate(NOW) == []


def test_high_5xx_rate_is_flagged() -> None:
    detector = _detector()
    _feed(detector, 90, status_code=200, duration_ms=50, ago_seconds=10)
    _feed(detector, 10, status_code=500, duration_ms=50, ago_seconds=10)  # 10% error rate

    findings = detector.evaluate(NOW)

    assert any(f.type == "5xx_rate" for f in findings)


def test_latency_spike_vs_baseline_is_flagged() -> None:
    detector = _detector()
    # Baseline window: older than 300s but within the 3600s baseline window.
    _feed(detector, 50, status_code=200, duration_ms=50, ago_seconds=600)
    # Recent window: within the last 300s, latency > 2x baseline.
    _feed(detector, 50, status_code=200, duration_ms=500, ago_seconds=10)

    findings = detector.evaluate(NOW)

    assert any(f.type == "latency_p95" for f in findings)


def test_samples_older_than_baseline_window_are_pruned() -> None:
    detector = _detector()
    _feed(detector, 50, status_code=200, duration_ms=50, ago_seconds=10_000)  # way past baseline
    _feed(detector, 50, status_code=200, duration_ms=50, ago_seconds=10)

    detector.evaluate(NOW)

    assert all(s.occurred_at >= NOW - timedelta(seconds=3600) for s in detector._samples)
