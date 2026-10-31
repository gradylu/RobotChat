"""ROS2 桥接音频适配（T-1.8，方案 B）：应用容器 ↔ 宿主机桥接节点（本地 WebSocket）。

协议（与 `deploy/bridge/robotchat_bridge.py` 对齐）：
- 二进制帧：原始 PCM16LE（单声道），采样率由配置约定
- 文本帧：JSON 控制指令，目前支持 `{"cmd": "flush"}`
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from robotchat.audio.frame import AudioChunk, AudioFrame
from robotchat.observability.logging import get_logger
from robotchat.providers.registry import register_provider

logger = get_logger("robotchat.audio.bridge")


@register_provider("transport", "ros2_bridge")
class Ros2BridgeTransport:
    name = "ros2_bridge"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.url = str(cfg.get("bridge_url") or "ws://127.0.0.1:9100/audio")
        self.sample_rate = int(cfg.get("sample_rate", 16000))
        self.channels = int(cfg.get("channels", 1))
        self.reconnect_delay_s = float(cfg.get("reconnect_delay_s", 0.5))
        self.played_chunks = 0
        self.reconnects = 0

        self._websockets: Any = None
        self._ws: Any = None
        self._running = False
        self._seq = 0

    async def open(self) -> None:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise RuntimeError("websockets 未安装，无法使用 ros2_bridge 传输") from exc
        self._websockets = websockets
        self._ws = await websockets.connect(self.url, max_size=None)
        self._running = True
        logger.info("bridge_transport_open", url=self.url)

    async def _reconnect(self) -> None:
        self.reconnects += 1
        await asyncio.sleep(self.reconnect_delay_s)
        try:
            self._ws = await self._websockets.connect(self.url, max_size=None)
            logger.warning("bridge_transport_reconnected", url=self.url, count=self.reconnects)
        except Exception:
            logger.exception("bridge_reconnect_failed", url=self.url)

    def capture(self) -> AsyncIterator[AudioFrame]:
        async def generator() -> AsyncIterator[AudioFrame]:
            while self._running:
                if self._ws is None:
                    await self._reconnect()
                    if self._ws is None:
                        continue
                try:
                    message = await asyncio.wait_for(self._ws.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    logger.warning("bridge_capture_disconnected", url=self.url)
                    self._ws = None
                    continue

                if not isinstance(message, bytes):
                    logger.debug("bridge_control_message", payload=message)
                    continue

                self._seq += 1
                yield AudioFrame(
                    data=message,
                    sample_rate=self.sample_rate,
                    channels=self.channels,
                    seq=self._seq,
                    ts_device=time.monotonic(),
                )

        return generator()

    async def play(self, chunk: AudioChunk) -> None:
        if self._ws is None:
            raise RuntimeError("桥接通道未连接：请先调用 open() 或等待重连")
        await self._ws.send(chunk.data)
        self.played_chunks += 1

    async def flush(self) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps({"cmd": "flush"}))

    async def close(self) -> None:
        self._running = False
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()
