"""类型化事件模型与有界异步事件总线（T-3.1、INV-4、INV-7、ADR-08）。

- 事件类型与 design §5.2 一一对应，避免散落字符串；
- 总线有界，超限按「丢弃新事件 + 告警计数」策略处理；
- 单写者串行处理：一个消费协程按入队顺序依次分发，转移无竞态；
- 单个订阅者异常被隔离，不影响其他订阅者与后续事件。
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from robotchat.observability.logging import get_logger

logger = get_logger("robotchat.core.events")


class EventType(str, Enum):
    """状态机事件（design §5.2）。"""

    # 生命周期
    EV_START = "EV_START"
    EV_READY = "EV_READY"
    EV_BOOT_FAIL = "EV_BOOT_FAIL"
    EV_SESSION_END = "EV_SESSION_END"
    # 音频 / VAD
    EV_SPEECH_START = "EV_SPEECH_START"
    EV_SPEECH_END = "EV_SPEECH_END"
    EV_SILENCE = "EV_SILENCE"
    # ASR
    EV_ASR_PARTIAL = "EV_ASR_PARTIAL"
    EV_ASR_FINAL = "EV_ASR_FINAL"
    EV_ASR_ERROR = "EV_ASR_ERROR"
    # 唤醒词
    EV_WAKE_DETECTED = "EV_WAKE_DETECTED"
    EV_WAKE_ACK_DONE = "EV_WAKE_ACK_DONE"
    EV_SESSION_ACTIVE_TIMEOUT = "EV_SESSION_ACTIVE_TIMEOUT"
    # LLM
    EV_LLM_FIRST_TOKEN = "EV_LLM_FIRST_TOKEN"
    EV_LLM_DELTA = "EV_LLM_DELTA"
    EV_LLM_TOOL_CALL = "EV_LLM_TOOL_CALL"
    EV_LLM_DONE = "EV_LLM_DONE"
    EV_LLM_ERROR = "EV_LLM_ERROR"
    # TTS / 播放
    EV_TTS_FIRST_CHUNK = "EV_TTS_FIRST_CHUNK"
    EV_TTS_DONE = "EV_TTS_DONE"
    EV_TTS_ERROR = "EV_TTS_ERROR"
    EV_PLAYBACK_DONE = "EV_PLAYBACK_DONE"
    EV_PLAYBACK_ERROR = "EV_PLAYBACK_ERROR"
    # 打断
    EV_BARGE_IN = "EV_BARGE_IN"
    EV_INTERRUPT_DONE = "EV_INTERRUPT_DONE"
    # 工具
    EV_TOOL_DONE = "EV_TOOL_DONE"
    EV_TOOL_ERROR = "EV_TOOL_ERROR"
    EV_TOOL_TIMEOUT = "EV_TOOL_TIMEOUT"
    # 控制
    EV_CANCEL = "EV_CANCEL"
    EV_RESET = "EV_RESET"
    EV_MUTE = "EV_MUTE"
    EV_UNMUTE = "EV_UNMUTE"
    EV_TEXT_INPUT = "EV_TEXT_INPUT"
    EV_SESSION_START = "EV_SESSION_START"
    # 健康
    EV_TIMEOUT = "EV_TIMEOUT"
    EV_HEALTH_FAIL = "EV_HEALTH_FAIL"
    EV_HEALTH_OK = "EV_HEALTH_OK"
    EV_WATCHDOG_TICK = "EV_WATCHDOG_TICK"
    EV_RECOVERED = "EV_RECOVERED"


@dataclass(frozen=True, slots=True)
class Event:
    """总线中的一条类型化事件。"""

    type: EventType
    turn_id: int | None = None
    session_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0

    def with_timestamp(self) -> Event:
        if self.ts:
            return self
        return Event(
            type=self.type,
            turn_id=self.turn_id,
            session_id=self.session_id,
            payload=self.payload,
            ts=time.time(),
        )


EventHandler = Callable[[Event], Awaitable[None] | None]


class BoundedEventBus:
    """有界、单写者串行的异步事件总线。"""

    def __init__(self, maxsize: int = 1024, *, name: str = "events") -> None:
        if maxsize < 1:
            raise ValueError(f"maxsize 必须 >= 1：{maxsize}")
        self.name = name
        self.maxsize = maxsize
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._handlers: list[EventHandler] = []
        self._task: asyncio.Task[None] | None = None
        self._running = False

        self.published = 0
        self.processed = 0
        self.dropped = 0
        self.errors = 0
        self.dropped_by_type: Counter[str] = Counter()

    # ------------------------------------------------------------ 订阅

    def subscribe(self, handler: EventHandler) -> None:
        if handler not in self._handlers:
            self._handlers.append(handler)

    def unsubscribe(self, handler: EventHandler) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    # ------------------------------------------------------------ 写入

    def publish(self, event: Event) -> bool:
        """非阻塞入队；队列满则丢弃当前事件并计数（INV-7）。"""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            self.dropped_by_type[event.type.value] += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                logger.warning(
                    "event_bus_overflow",
                    bus=self.name,
                    dropped=self.dropped,
                    event_type=event.type.value,
                )
            return False
        self.published += 1
        return True

    # ------------------------------------------------------------ 消费

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name=f"event-bus-{self.name}")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while self._running:
            try:
                event = await self._queue.get()
            except asyncio.CancelledError:
                raise
            await self._dispatch(event)

    async def process_one(self) -> bool:
        """处理队列头部的一条事件；空队列返回 False（便于确定性单测）。"""
        try:
            event = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return False
        await self._dispatch(event)
        return True

    async def drain(self, limit: int | None = None) -> int:
        """同步清空队列（供关停前收尾）；返回处理条数。"""
        count = 0
        while limit is None or count < limit:
            if not await self.process_one():
                break
            count += 1
        return count

    async def _dispatch(self, event: Event) -> None:
        self.processed += 1
        for handler in tuple(self._handlers):
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                self.errors += 1
                logger.exception(
                    "event_handler_failed",
                    bus=self.name,
                    event_type=event.type.value,
                    handler=getattr(handler, "__name__", repr(handler)),
                )

    # ------------------------------------------------------------ 观测

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def snapshot(self) -> dict[str, Any]:
        return {
            "bus": self.name,
            "maxsize": self.maxsize,
            "pending": self.pending,
            "published": self.published,
            "processed": self.processed,
            "dropped": self.dropped,
            "errors": self.errors,
        }


__all__ = ["Event", "EventType", "BoundedEventBus", "EventHandler"]
