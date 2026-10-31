"""T-1.7 管线单测：采集/播放并发互不阻塞（INV-3）、flush 立即静音、队列有界（INV-7）。"""

from __future__ import annotations

import asyncio

from robotchat.audio.frame import AudioChunk, AudioFrame
from robotchat.audio.pipeline import AudioPipeline

FRAME = 320
RATE = 16000


def _chunk(samples: int = FRAME) -> AudioChunk:
    return AudioChunk(data=b"\x00\x00" * samples, sample_rate=RATE, channels=1)


class FakeTransport:
    def __init__(
        self,
        frames: int = 200,
        interval: float = 0.0,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.frames = frames
        self.interval = interval
        self.gate = gate
        self.played: list[AudioChunk] = []
        self.flush_count = 0
        self.opened = False
        self.closed = False

    async def open(self) -> None:
        self.opened = True

    def capture(self):
        async def generator():
            for seq in range(self.frames):
                yield AudioFrame(
                    data=b"\x00\x00" * FRAME,
                    sample_rate=RATE,
                    seq=seq,
                    ts_device=seq * 0.02,
                )
                await asyncio.sleep(self.interval)

        return generator()

    async def play(self, chunk: AudioChunk) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.played.append(chunk)

    async def flush(self) -> None:
        self.flush_count += 1

    async def close(self) -> None:
        self.closed = True


async def test_capture_runs_while_playback_is_blocked() -> None:
    gate = asyncio.Event()
    transport = FakeTransport(frames=200, gate=gate)
    pipeline = AudioPipeline(transport)

    await pipeline.start()
    await pipeline.play(_chunk())
    await asyncio.sleep(0.05)

    # 播放被设备侧阻塞，采集任务仍持续出帧（INV-3）
    assert pipeline.captured_frames >= 10
    assert transport.played == []

    gate.set()
    await asyncio.sleep(0.02)
    assert len(transport.played) == 1

    await pipeline.stop()
    assert transport.closed is True


async def test_flush_clears_pending_queue() -> None:
    transport = FakeTransport()
    pipeline = AudioPipeline(transport)
    for _ in range(5):
        await pipeline.play(_chunk())

    assert pipeline.pending_chunks == 5
    await pipeline.flush()

    assert pipeline.pending_chunks == 0
    assert pipeline.pending_bytes == 0
    assert transport.flush_count == 1


async def test_oversized_chunk_is_dropped() -> None:
    pipeline = AudioPipeline(FakeTransport())
    # queue_ms=300 → 上限 9600B；12000B 单块直接丢弃
    await pipeline.play(_chunk(samples=6000))

    assert pipeline.dropped_chunks == 1
    assert pipeline.pending_chunks == 0


async def test_queue_overflow_drops_oldest() -> None:
    pipeline = AudioPipeline(FakeTransport())
    for _ in range(20):
        await pipeline.play(_chunk())

    assert pipeline.pending_bytes <= 9600
    assert pipeline.dropped_chunks >= 1
