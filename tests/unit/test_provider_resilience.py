"""Provider 熔断 / 半开回切 / 离线锁定 / 预热单测（T-2.6、T-2.7、T-2.10）。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from robotchat.health import HealthStatus
from robotchat.providers.registry import (
    CircuitBreaker,
    ProviderRegistry,
    ProviderUnavailableError,
    ResiliencePolicy,
    register_provider,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@register_provider("asr", "resilience_primary")
class _ResilientPrimary:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.name = "resilience_primary"

    async def health(self) -> HealthStatus:
        if self.config.get("unhealthy"):
            return HealthStatus.unavailable("down")
        return HealthStatus.ok("up")


@register_provider("asr", "resilience_fallback")
class _ResilientFallback(_ResilientPrimary):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.name = "resilience_fallback"


def test_breaker_opens_after_threshold() -> None:
    breaker = CircuitBreaker(ResiliencePolicy(failure_threshold=3, cooldown_s=30))
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == "OPEN"
    allowed, probe = breaker.try_acquire()
    assert (allowed, probe) == (False, False)


def test_breaker_half_open_single_probe_then_close() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(ResiliencePolicy(failure_threshold=1, cooldown_s=5), clock)
    breaker.record_failure()
    assert breaker.state == "OPEN"

    clock.now = 5.0
    assert breaker.state == "HALF_OPEN"
    assert breaker.try_acquire() == (True, True)
    # 半开期间只放行一个探测请求，避免恢复瞬间打满主驱动
    assert breaker.try_acquire() == (False, False)

    breaker.record_success()
    assert breaker.state == "CLOSED"


def test_breaker_probe_failure_reopens() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(ResiliencePolicy(failure_threshold=1, cooldown_s=5), clock)
    breaker.record_failure()
    clock.now = 5.0
    assert breaker.try_acquire() == (True, True)
    breaker.record_failure()
    assert breaker.state == "OPEN"
    assert breaker.snapshot()["cooldown_remaining_s"] > 0


async def test_slot_switches_to_fallback_when_open() -> None:
    clock = FakeClock()
    registry = ProviderRegistry(ResiliencePolicy(failure_threshold=2, cooldown_s=10), clock)
    slot = registry.register_slot("asr", "resilience_primary", {}, "resilience_fallback", {})

    slot.record_failure("primary down")
    slot.record_failure("primary down")
    assert slot.breaker.state == "OPEN"

    entry = await registry.acquire("asr")
    assert entry.name == "resilience_fallback"
    assert slot.effective_name == "resilience_fallback"


async def test_slot_probes_primary_after_cooldown_and_recovers() -> None:
    clock = FakeClock()
    registry = ProviderRegistry(ResiliencePolicy(failure_threshold=1, cooldown_s=5), clock)
    slot = registry.register_slot("asr", "resilience_primary", {}, "resilience_fallback", {})

    slot.record_failure("down")
    assert (await registry.acquire("asr")).name == "resilience_fallback"

    clock.now = 5.0
    entry = await registry.acquire("asr")
    assert entry.name == "resilience_primary"
    assert slot.breaker.state == "CLOSED"


async def test_offline_locks_cloud_driver() -> None:
    registry = ProviderRegistry()
    registry.register_slot(
        "asr", "resilience_primary", {"cloud": True}, "resilience_fallback", {}
    )
    registry.set_offline(True, "公网不可达")

    slot = registry.slot("asr")
    assert slot is not None
    assert slot.primary.locked
    assert slot.fallback is not None and slot.fallback.usable
    assert (await registry.acquire("asr")).name == "resilience_fallback"

    registry.set_offline(False)
    assert not slot.primary.locked


async def test_detect_offline_marks_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = ProviderRegistry()
    registry.register_slot(
        "asr", "resilience_primary", {"cloud": True}, "resilience_fallback", {}
    )
    offline, detail = await registry.detect_offline(
        "https://example.com", 100, client=client
    )
    await client.aclose()
    assert offline is True
    assert "公网不可达" in detail
    assert registry.snapshot()["asr"]["primary"]["locked"] is True


async def test_warmup_records_driver_health() -> None:
    registry = ProviderRegistry(ResiliencePolicy(warmup_timeout_s=1))
    registry.register_slot("asr", "resilience_primary", {}, "resilience_fallback", {})
    result = await registry.warmup_all()
    drivers = result["asr"]["drivers"]
    assert [item["name"] for item in drivers] == ["resilience_primary", "resilience_fallback"]
    assert all(item["ok"] for item in drivers)


async def test_unregistered_slot_raises() -> None:
    registry = ProviderRegistry()
    with pytest.raises(ProviderUnavailableError, match="未注册"):
        await registry.acquire("tts")


async def test_both_drivers_unusable_raises() -> None:
    registry = ProviderRegistry()
    registry.register_slot("tts", "missing_driver", {}, None)
    with pytest.raises(ProviderUnavailableError, match="不可用"):
        await registry.acquire("tts")
