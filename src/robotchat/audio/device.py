"""AudioDeviceAdapter 抽象：采集 / 播放两契约（T-1A.1 / FR-16.1、design §3.6.1）。

上层（VAD / KWS / ASR / 播放队列 / Barge-in）只依赖契约实例，
不得出现具体话题名、服务名或消息类型（FR-16.7 / CON-11）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from robotchat.audio.frame import AudioChunk, AudioFrame


class AudioCaptureContract(Protocol):
    async def open(self) -> None: ...

    def stream(self) -> AsyncIterator[AudioFrame]:
        """统一为内部管线的 16kHz / 16bit / 单声道（或由适配层转换）。"""

    def last_timestamp(self) -> float:
        """最近一帧的单调递增时间戳（秒），供回声剔除与打断判定。"""

    async def close(self) -> None: ...


class AudioPlaybackContract(Protocol):
    async def open(self) -> None: ...

    async def play(self, chunk: AudioChunk) -> None:
        """背压有界；返回即代表设备侧已接受。"""

    async def flush(self) -> None:
        """立即静音：返回后设备侧不再输出残留音频（Barge-in 300ms 预算内）。"""

    async def set_volume(self, level: float) -> None: ...

    async def close(self) -> None: ...


class AudioDeviceAdapter(Protocol):
    """机型适配器：持有采集与播放两个契约实例。"""

    profile_name: str
    capture: AudioCaptureContract
    playback: AudioPlaybackContract

    async def open(self) -> None: ...

    async def close(self) -> None: ...


class AdapterAudioTransport:
    """把设备适配器组合成 `AudioTransport`，使现有管线无感接入（T-1A.1）。

    业务层通过 `AudioTransport` 只看到 `AudioFrame` / `AudioChunk`，
    机型差异全部留在适配器与 Profile 内。
    """

    def __init__(self, adapter: AudioDeviceAdapter) -> None:
        self._adapter = adapter
        self.name = f"device:{adapter.profile_name}"
        self._opened = False

    @property
    def adapter(self) -> AudioDeviceAdapter:
        return self._adapter

    async def open(self) -> None:
        await self._adapter.open()
        self._opened = True

    def capture(self) -> AsyncIterator[AudioFrame]:
        return self._adapter.capture.stream()

    async def play(self, chunk: AudioChunk) -> None:
        await self._adapter.playback.play(chunk)

    async def flush(self) -> None:
        await self._adapter.playback.flush()

    async def close(self) -> None:
        if self._opened:
            await self._adapter.close()
            self._opened = False

    async def set_volume(self, level: float) -> None:
        await self._adapter.playback.set_volume(level)


__all__ = [
    "AdapterAudioTransport",
    "AudioCaptureContract",
    "AudioDeviceAdapter",
    "AudioPlaybackContract",
]
