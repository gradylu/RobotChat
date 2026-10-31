"""看门狗（T-3.5、C-22、C-27、INV-2、FR-7.2、FR-7.7）。

两层保护：
1. 每状态超时：非常驻状态超过注册表声明的超时 → 触发 `on_timeout`（编排层投递 `EV_TIMEOUT`）；
2. 全局心跳：默认 1s，检测「无任何进展」→ 触发 `on_stall` 自恢复。

进展判据（任一即可）：音频帧到达、VAD / KWS 推理产出、事件被处理、provider 健康探测返回。
`IDLE` 与常开聆听 `ACTIVE_IDLE` 是常驻等待，不做超时与无进展判定，避免静置误触发。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from robotchat.core.state_machine import State, StateMachine
from robotchat.observability.logging import get_logger

logger = get_logger("robotchat.core.watchdog")

TimeoutHook = Callable[[State], Awaitable[None]]
StallHook = Callable[[str], Awaitable[None]]


class Watchdog:
    def __init__(
        self,
        machine: StateMachine,
        *,
        heartbeat_ms: int = 1000,
        on_timeout: TimeoutHook | None = None,
        on_stall: StallHook | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if heartbeat_ms <= 0:
            raise ValueError("heartbeat_ms 必须为正数")
        self.machine = machine
        self.heartbeat_ms = heartbeat_ms
        self.on_timeout = on_timeout
        self.on_stall = on_stall
        self._clock = clock

        now = clock()
        self._last_state: State = machine.state
        self._state_entered = now
        self._last_progress = now
        self._timeout_fired = False
        self._stall_fired = False

        self._running = False
        self._task: asyncio.Task[None] | None = None

        self.ticks = 0
        self.timeouts = 0
        self.stalls = 0
        self.last_progress_source = ""

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="watchdog")
        logger.info("watchdog_started", heartbeat_ms=self.heartbeat_ms)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        interval = max(0.05, self.heartbeat_ms / 4000.0)
        while self._running:
            await asyncio.sleep(interval)
            await self.tick()

    # ------------------------------------------------------------ 判定

    def mark_progress(self, source: str = "") -> None:
        """记录一次进展；任何判据命中都刷新心跳。"""
        self._last_progress = self._clock()
        self.last_progress_source = source
        self._stall_fired = False

    async def tick(self) -> None:
        """一次心跳检查（单测可注入时钟后手动调用）。"""
        self.ticks += 1
        now = self._clock()
        state = self.machine.state

        if state != self._last_state:
            self._last_state = state
            self._state_entered = now
            self._timeout_fired = False
            self._stall_fired = False
            self._last_progress = now

        if self.machine.is_resident(state):
            return

        await self._check_timeout(state, now)
        await self._check_stall(state, now)

    async def _check_timeout(self, state: State, now: float) -> None:
        timeout_ms = self.machine.timeout_ms(state)
        if timeout_ms is None or self._timeout_fired:
            return
        if now - self._state_entered < timeout_ms / 1000.0:
            return
        self._timeout_fired = True
        self.timeouts += 1
        logger.warning("watchdog_state_timeout", state=state.value, timeout_ms=timeout_ms)
        if self.on_timeout is not None:
            await self.on_timeout(state)

    async def _check_stall(self, state: State, now: float) -> None:
        if self._stall_fired or self.on_stall is None:
            return
        if now - self._last_progress < self.heartbeat_ms / 1000.0:
            return
        self._stall_fired = True
        self.stalls += 1
        reason = f"{state.value} 持续 {self.heartbeat_ms}ms 无进展"
        logger.error("watchdog_no_progress", state=state.value, reason=reason)
        await self.on_stall(reason)

    # ------------------------------------------------------------ 观测

    def snapshot(self) -> dict[str, object]:
        return {
            "state": self.machine.state.value,
            "heartbeat_ms": self.heartbeat_ms,
            "ticks": self.ticks,
            "timeouts": self.timeouts,
            "stalls": self.stalls,
            "last_progress_source": self.last_progress_source,
        }


__all__ = ["Watchdog", "TimeoutHook", "StallHook"]
