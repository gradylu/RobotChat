"""AudioTransport 抽象：容器如何触达设备（DDS / 桥接 / 文件），与机型无关（ADR-02）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from robotchat.audio.frame import AudioChunk, AudioFrame


class AudioTransport(Protocol):
    """采集与播放为两条独立链路，不得相互阻塞（FR-1.3）。"""

    async def open(self) -> None: ...

    def capture(self) -> AsyncIterator[AudioFrame]: ...

    async def play(self, chunk: AudioChunk) -> None: ...

    async def flush(self) -> None:
        """立即静音：返回后设备侧不再输出残留音频（Barge-in 300ms 预算内）。"""

    async def close(self) -> None: ...
