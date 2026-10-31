"""T-1.9 音频诊断单测：probe 帧率判定、loopback 互相关。"""

from __future__ import annotations

import asyncio

from robotchat.audio.frame import AudioChunk, AudioFrame
from robotchat.observability.diagnostics import loopback, probe

FRAME = 320
RATE = 16000


class ProbeTransport:
    def __init__(self, frames: int) -> None:
        self.frames = frames
        self.closed = False

    async def open(self) -> None:
        return None

    def capture(self):
        async def generator():
            for seq in range(self.frames):
                yield AudioFrame(data=b"\x00\x00" * FRAME, sample_rate=RATE, seq=seq)

        return generator()

    async def play(self, chunk: AudioChunk) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class EchoTransport:
    """播放即回环到采集的自测通道（模拟物理回环）。"""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.played_chunks = 0
        self.flush_count = 0
        self.closed = False

    async def open(self) -> None:
        return None

    def capture(self):
        async def generator():
            seq = 0
            while True:
                data = await self.queue.get()
                seq += 1
                yield AudioFrame(data=data, sample_rate=RATE, seq=seq)

        return generator()

    async def play(self, chunk: AudioChunk) -> None:
        self.played_chunks += 1
        await self.queue.put(chunk.data)

    async def flush(self) -> None:
        self.flush_count += 1

    async def close(self) -> None:
        self.closed = True


async def test_probe_reports_live_capture() -> None:
    transport = ProbeTransport(frames=60)
    result = await probe(transport, duration_s=0.01)

    assert result.frames == 60
    assert result.ok is True
    assert result.frames_per_second > 0
    assert transport.closed is True


async def test_probe_flags_silent_channel() -> None:
    result = await probe(ProbeTransport(frames=0), duration_s=0.01)

    assert result.frames == 0
    assert result.ok is False
    assert "未收到音频" in result.detail


async def test_loopback_detects_reference_tone() -> None:
    transport = EchoTransport()
    result = await loopback(transport, duration_s=0.2)

    assert result.ok is True
    assert result.correlation >= 0.5
    assert result.played_chunks > 0
    assert result.captured_frames > 0
