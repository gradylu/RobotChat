"""延迟指标与分位数单测（T-0.6）。"""

from __future__ import annotations

import pytest

from robotchat.observability.metrics import (
    E2E_FIRST_RESPONSE,
    ENDPOINT,
    MetricsCollector,
    percentile,
)


def test_percentile_linear_interpolation() -> None:
    values = list(range(1, 101))
    assert percentile(values, 50) == 50.5
    assert round(percentile(values, 90), 2) == 90.1
    assert round(percentile(values, 95), 2) == 95.05
    assert percentile([], 95) == 0.0
    assert percentile([42], 95) == 42.0


def test_endpoint_and_first_response_are_independent() -> None:
    collector = MetricsCollector(buffer_per_stage=100)
    collector.record(ENDPOINT, 650)
    collector.record(ENDPOINT, 700)
    collector.record(E2E_FIRST_RESPONSE, 1500)

    endpoint = collector.summary(ENDPOINT)
    e2e = collector.summary(E2E_FIRST_RESPONSE)
    assert endpoint.count == 2
    # 线性插值：650 + (700-650) * 0.95 = 697.5
    assert endpoint.p95 == pytest.approx(697.5)
    assert e2e.count == 1
    assert e2e.p95 == 1500


def test_buffer_is_bounded() -> None:
    collector = MetricsCollector(buffer_per_stage=3)
    for value in (10, 20, 30, 40, 50):
        collector.record(E2E_FIRST_RESPONSE, value)
    summary = collector.summary(E2E_FIRST_RESPONSE)
    assert summary.count == 3
    assert summary.minimum == 30
    assert summary.maximum == 50


def test_snapshot_shape() -> None:
    collector = MetricsCollector()
    collector.record(ENDPOINT, 500, session_id="s1", provider="vad")
    snapshot = collector.snapshot()
    assert snapshot[ENDPOINT]["count"] == 1
    assert set(snapshot[ENDPOINT]) >= {"p50", "p90", "p95", "p99", "min", "max"}


def test_negative_latency_rejected() -> None:
    collector = MetricsCollector()
    try:
        collector.record(ENDPOINT, -1)
    except ValueError:
        return
    raise AssertionError("负延迟应被拒绝")
