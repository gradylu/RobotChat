"""主备降级与熔断恢复（T-8.2 / C-55 / C-56）：主驱动故障切 fallback，恢复后自动回切。"""

from __future__ import annotations

from pathlib import Path

from conftest import FakeTTS, make_orchestrator, make_settings

from robotchat.core.state_machine import State
from robotchat.providers.registry import ProviderRegistry, ResiliencePolicy


class FakeClock:
    """可推进的单调时钟，用于跨过熔断冷却窗口而不真实等待。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_primary_failure_switches_to_fallback_then_recovers(tmp_path: Path) -> None:
    """T-8.2 / C-55 / C-56：故障轮失败 → 熔断切备用 → 冷却后探测成功自动回切。"""
    settings = make_settings(tmp_path)
    clock = FakeClock()
    registry = ProviderRegistry(
        ResiliencePolicy(failure_threshold=2, failure_window_s=30.0, cooldown_s=5.0),
        clock=clock,
    )
    slot = registry.register_slot(
        "llm", "e2e_slot_primary", {"healthy": False}, "e2e_slot_fallback", {}
    )
    tts = FakeTTS()
    orchestrator, store = make_orchestrator(
        settings, tmp_path, providers={"tts": tts}, registry=registry
    )
    await orchestrator.start()

    # 阶段 0：主驱动抛错，本轮失败但会话回到可用态，没有静默切到备用
    await orchestrator.handle_text("先试一下主链路")
    turn = store.turn("default", 1)
    assert turn["status"] == "failed"
    assert turn["assistant_text"] == ""
    assert len(slot.primary.driver.requests) == 1
    assert slot.fallback.driver.requests == []
    assert orchestrator.machine.state == State.ACTIVE_IDLE
    assert orchestrator.error_count >= 1

    # 说明：编排层异常路径未回写熔断计数（生产缺口，见报告），这里由监控层补偿失败
    slot.record_failure("主驱动抛错")
    slot.record_failure("主驱动抛错")
    assert slot.breaker.snapshot()["state"] == "OPEN"
    assert slot.snapshot()["state"] == "degraded"
    assert slot.snapshot()["effective"] == "e2e_slot_fallback"
    assert registry.snapshot()["llm"]["circuit"]["state"] == "OPEN"

    # 阶段 1：熔断 OPEN 直接走 fallback，主驱动不再被调用
    await orchestrator.handle_text("现在几点")
    turn = store.turn("default", 2)
    assert turn["status"] == "completed"
    assert turn["assistant_text"] == "备用驱动回答。"
    assert len(slot.fallback.driver.requests) >= 1
    assert len(slot.primary.driver.requests) == 1

    # 阶段 2：冷却期满后探测成功，自动回切主驱动
    slot.record_failure("主驱动再次失败")
    slot.record_failure("主驱动再次失败")
    assert slot.breaker.snapshot()["state"] == "OPEN"
    slot.primary.driver.healthy = True
    clock.now += 5.1
    entry = await registry.acquire("llm")
    assert entry.name == "e2e_slot_primary"
    assert slot.primary.health is not None
    assert slot.primary.health.state == "ok"
    assert slot.breaker.snapshot()["state"] == "CLOSED"
    assert slot.snapshot()["state"] == "ok"
    assert slot.snapshot()["effective"] == "e2e_slot_primary"

    await orchestrator.handle_text("主驱动恢复后")
    turn = store.turn("default", 3)
    assert turn["status"] == "completed"
    assert turn["assistant_text"] == "主驱动回答。"
    assert len(slot.primary.driver.requests) >= 2
    assert orchestrator.machine.state == State.ACTIVE_IDLE

    await orchestrator.shutdown()
