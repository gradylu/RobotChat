"""待机态 KWS 调度（T-3A.3、C-144 / C-148、INV-9）。

核心原则：**KWS 只在待机态（`IDLE`）运行**；进入活跃态即停止，唤醒不进入打断链路。
调度器只负责「按状态启停 + 帧喂入 + 命中回调 + 命中计数」，阈值 / 冷却 / 二次确认
由驱动实现（T-3A.2 / T-3A.6）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from robotchat.audio.frame import AudioFrame
from robotchat.core.cancel import CancellationToken
from robotchat.observability.logging import get_logger
from robotchat.observability.metrics import WAKE_COUNT, MetricsCollector
from robotchat.providers.base import WakeEvent, WakeWordProvider

logger = get_logger("robotchat.audio.kws")

WakeHandler = Callable[[WakeEvent], Any]


class KwsScheduler:
    """待机唤醒调度：`start()` 建流，`feed()` 喂帧，`stop()` 释放模型会话。"""

    def __init__(
        self,
        provider: WakeWordProvider,
        *,
        on_wake: WakeHandler,
        is_standby: Callable[[], bool] | None = None,
        metrics: MetricsCollector | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self._on_wake = on_wake
        self._is_standby = is_standby or (lambda: True)
        self._metrics = metrics
        self._clock = clock

        self._token = CancellationToken("kws")
        self._stream: Any | None = None
        self._events_task: asyncio.Task[Any] | None = None
        self._handler_tasks: set[asyncio.Task[Any]] = set()
        self._running = False

        self.frames_fed = 0
        self.frames_skipped = 0
        self.wake_events = 0
        self.last_wake_at: float | None = None
        self.error = ""

    @property
    def running(self) -> bool:
        return self._running

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._running:
            return
        try:
            self._token = CancellationToken("kws")
            self._stream = await self.provider.open_stream(self._token)
            self._events_task = asyncio.create_task(
                self._consume_events(), name="kws-events"
            )
            self._running = True
            self.error = ""
            logger.info("kws_started", provider=getattr(self.provider, "name", "kws"))
        except Exception as exc:
            self._running = False
            self.error = repr(exc)
            logger.warning("kws_start_failed", detail=self.error)

    async def stop(self) -> None:
        if not self._running and self._stream is None:
            return
        self._running = False
        self._token.cancel("kws_stop")
        task = self._events_task
        self._events_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                await stream.close()
            except Exception:
                logger.warning("kws_stream_close_failed")
        logger.info(
            "kws_stopped",
            frames=self.frames_fed,
            wakes=self.wake_events,
        )

    async def feed(self, frame: AudioFrame) -> None:
        """仅待机态喂帧；活跃态静默丢弃（保证 KWS 不参与打断链路）。"""
        if not self._running or self._stream is None or not self._is_standby():
            self.frames_skipped += 1
            return
        self.frames_fed += 1
        try:
            await self._stream.feed(frame)
        except Exception as exc:
            self.error = repr(exc)
            logger.exception("kws_feed_failed")

    # ------------------------------------------------------------ 命中

    async def _consume_events(self) -> None:
        try:
            async for event in self.provider.events():
                if event.type == "error":
                    self.error = event.error or "KWS 推理错误"
                    logger.warning("kws_event_error", detail=self.error)
                    continue
                self.wake_events += 1
                self.last_wake_at = self._clock()
                if self._metrics is not None:
                    self._metrics.increment(WAKE_COUNT)
                # 命中回调单独起任务：回调通常会触发状态转移并停止 KWS，
                # 若在事件消费协程内同步等待会取消自身（INV-9）。
                task = asyncio.create_task(
                    self._dispatch_wake(event), name="kws-wake-handler"
                )
                self._handler_tasks.add(task)
                task.add_done_callback(self._handler_tasks.discard)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("kws_event_consumer_failed")

    async def _dispatch_wake(self, event: WakeEvent) -> None:
        try:
            result = self._on_wake(event)
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("wake_handler_failed")

    # ------------------------------------------------------------ 观测

    def snapshot(self) -> dict[str, Any]:
        driver = getattr(self.provider, "snapshot", None)
        return {
            "running": self._running,
            "provider": getattr(self.provider, "name", ""),
            "frames_fed": self.frames_fed,
            "frames_skipped": self.frames_skipped,
            "wake_events": self.wake_events,
            "last_wake_at": self.last_wake_at,
            "error": self.error,
            "driver": driver() if callable(driver) else {},
        }


__all__ = ["KwsScheduler", "WakeHandler"]
