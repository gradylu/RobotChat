"""WebSocket 连接工具（ASR / TTS 云端驱动共用）。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

ConnectFactory = Callable[[str, dict[str, str]], Awaitable[Any]]


async def default_connect(url: str, headers: dict[str, str]) -> Any:
    from websockets.asyncio.client import connect as ws_connect

    return await ws_connect(
        url,
        additional_headers=headers or None,
        open_timeout=3.0,
        ping_interval=20.0,
        close_timeout=1.0,
    )


__all__ = ["ConnectFactory", "default_connect"]
