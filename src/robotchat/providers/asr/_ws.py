"""流式 ASR 驱动共用的 WebSocket 会话（FunASR 与 DashScope 仅协议报文不同）。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from robotchat.audio.frame import AudioFrame
from robotchat.providers._ws import ConnectFactory, default_connect
from robotchat.providers.base import ASREvent

Mapper = Callable[[dict[str, Any]], ASREvent | None]


class WebSocketASRStream:
    """把 bytes 音频写入连接、把服务端 JSON 映射为 partial / final 事件。

    `close()` 只发送结束报文，之后由 `events()` 消费剩余结果并在收到 final 后关闭连接；
    若调用方不消费事件，`final_timeout_s` 后强制关闭，避免连接泄漏。
    """

    def __init__(
        self,
        ws: Any,
        mapper: Mapper,
        finish_payload: dict[str, Any] | None,
        *,
        final_timeout_s: float = 2.0,
    ) -> None:
        self._ws = ws
        self._mapper = mapper
        self._finish_payload = finish_payload
        self._final_timeout_s = final_timeout_s
        self._closing = False
        self._ended = False
        self._close_task: asyncio.Task[None] | None = None

    async def feed(self, frame: AudioFrame) -> None:
        if self._closing:
            return
        await self._ws.send(frame.data)

    async def events(self) -> AsyncIterator[ASREvent]:
        try:
            while True:
                timeout = self._final_timeout_s if self._closing else None
                try:
                    raw = await asyncio.wait_for(self._ws.recv(), timeout)
                except asyncio.TimeoutError:
                    break
                except Exception as exc:
                    if not self._closing:
                        yield ASREvent(type="error", error=repr(exc), ts=time.time())
                    break
                if isinstance(raw, (bytes, bytearray)):
                    continue
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                event = self._mapper(message)
                if event is None:
                    continue
                yield event
                if event.type == "final":
                    break
        finally:
            self._ended = True
            if self._close_task is not None:
                self._close_task.cancel()
            with contextlib.suppress(Exception):  # 关闭失败不影响上层
                await self._ws.close()

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._finish_payload is not None:
            try:
                await self._ws.send(json.dumps(self._finish_payload, ensure_ascii=False))
            except Exception:  # 连接已断开时直接走清理
                self._ended = True
        if self._ended:
            with contextlib.suppress(Exception):
                await self._ws.close()
            return
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_after_timeout())

    async def _close_after_timeout(self) -> None:
        await asyncio.sleep(self._final_timeout_s)
        if not self._ended:
            with contextlib.suppress(Exception):
                await self._ws.close()


__all__ = ["WebSocketASRStream", "Mapper", "ConnectFactory", "default_connect"]
