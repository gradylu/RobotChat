"""音频管线编排（T-1.7）：采集与播放拆分为独立并发任务（INV-3、FR-1.3）。

- 采集任务：transport.capture → AEC → VAD → 回调；**永不因播报停止**
- 播放任务：有界队列 → transport.play；`flush()` 立即清空待播并停声
- 两者互不阻塞：播放慢不会让采集停滞，反之亦然
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections import deque
from collections.abc import Awaitable, Callable

import numpy as np

from robotchat.audio.aec import EchoCanceller, NullEchoCanceller
from robotchat.audio.buffer import float_to_pcm16, pcm16_to_float, resample_float
from robotchat.audio.frame import AudioChunk, AudioFrame
from robotchat.audio.transport import AudioTransport
from robotchat.config.schema import CaptureSettings, PlaybackSettings
from robotchat.observability.logging import get_logger
from robotchat.providers.base import VadEngine, VadEvent

logger = get_logger("robotchat.audio.pipeline")


class PlaybackReference:
    """最近播放音频的有界参考缓冲（软件 AEC 的回采参考，FR-1.5）。"""

    def __init__(self, capacity_samples: int) -> None:
        if capacity_samples <= 0:
            raise ValueError(f"capacity_samples 必须为正数：{capacity_samples}")
        self.capacity_samples = capacity_samples
        self._data = np.zeros(0, dtype=np.float32)

    def append(self, samples: np.ndarray) -> None:
        data = np.asarray(samples, dtype=np.float32).ravel()
        self._data = np.concatenate((self._data, data))[-self.capacity_samples :]

    def last(self, count: int, delay_samples: int = 0) -> np.ndarray:
        """取最近 count 个样本（delay_samples 为设备侧播放→采集的延迟补偿）。"""
        end = self._data.size - max(0, delay_samples)
        if end <= 0:
            return np.zeros(count, dtype=np.float32)
        start = max(0, end - count)
        window = self._data[start:end]
        if window.size < count:
            window = np.pad(window, (count - window.size, 0))
        return window

    def clear(self) -> None:
        self._data = np.zeros(0, dtype=np.float32)


class AudioPipeline:
    """采集 + 播放的双任务编排（不依赖状态机，可独立用于诊断与单测）。"""

    def __init__(
        self,
        transport: AudioTransport,
        *,
        vad: VadEngine | None = None,
        echo_canceller: EchoCanceller | None = None,
        capture: CaptureSettings | None = None,
        playback: PlaybackSettings | None = None,
        on_frame: Callable[[AudioFrame, np.ndarray], None | Awaitable[None]] | None = None,
        on_vad: Callable[[VadEvent], None | Awaitable[None]] | None = None,
        sample_rate: int = 16000,
        frame_ms: int = 20,
        reference_delay_ms: float = 0.0,
    ) -> None:
        self._transport = transport
        self._vad = vad
        self._aec = echo_canceller or NullEchoCanceller()
        self._capture = capture or CaptureSettings()
        self._playback = playback or PlaybackSettings()
        self._on_frame = on_frame
        self._on_vad = on_vad
        self._sample_rate = sample_rate
        self._frame_ms = frame_ms
        self._reference_delay_samples = int(sample_rate * reference_delay_ms / 1000)

        self._pending: deque[AudioChunk] = deque()
        self._pending_bytes = 0
        self._queue_limit_bytes = max(
            1, int(self._playback.queue_ms * sample_rate * 2 / 1000)
        )

        self._wakeup = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False
        self._playing = False
        self._reference = PlaybackReference(sample_rate * 2)

        self.captured_frames = 0
        self.played_chunks = 0
        self.dropped_chunks = 0
        self.vad_events = 0
        self.capture_errors = 0

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._running:
            return
        await self._transport.open()
        self._running = True
        self._tasks = [
            asyncio.create_task(self._capture_loop(), name="audio-capture"),
            asyncio.create_task(self._playback_loop(), name="audio-playback"),
        ]
        logger.info(
            "audio_pipeline_started",
            queue_limit_bytes=self._queue_limit_bytes,
            vad=bool(self._vad),
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._wakeup.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await self._transport.close()
        logger.info(
            "audio_pipeline_stopped",
            captured_frames=self.captured_frames,
            played_chunks=self.played_chunks,
            dropped_chunks=self.dropped_chunks,
        )

    # ------------------------------------------------------------ 播放侧

    async def play(self, chunk: AudioChunk) -> None:
        """非阻塞入队；超限丢最旧并计数（INV-7）。"""
        size = len(chunk.data)
        if size > self._queue_limit_bytes:
            self.dropped_chunks += 1
            logger.warning("playback_chunk_too_large", chunk_bytes=size)
            return
        self._pending.append(chunk)
        self._pending_bytes += size
        while self._pending_bytes > self._queue_limit_bytes and len(self._pending) > 1:
            old = self._pending.popleft()
            self._pending_bytes -= len(old.data)
            self.dropped_chunks += 1
            logger.warning("playback_queue_overflow", dropped_chunks=self.dropped_chunks)
        self._wakeup.set()

    async def flush(self) -> None:
        """立即静音：清空待播队列 + 调用设备侧停播（Barge-in 预算内）。"""
        dropped = len(self._pending)
        self._pending.clear()
        self._pending_bytes = 0
        await self._transport.flush()
        self._playing = False
        if dropped:
            logger.info("playback_flushed", dropped_chunks=dropped)

    @property
    def pending_chunks(self) -> int:
        return len(self._pending)

    @property
    def pending_bytes(self) -> int:
        return self._pending_bytes

    @property
    def idle(self) -> bool:
        return not self._pending and not self._playing

    async def wait_until_idle(self, timeout_s: float | None = None) -> bool:
        """等待播放队列排空且设备侧当前块播完；超时返回 False。"""
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            if self.idle:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.02)

    # ------------------------------------------------------------ 内部任务

    async def _capture_loop(self) -> None:
        try:
            async for frame in self._transport.capture():
                if not self._running:
                    break
                self.captured_frames += 1
                cleaned = self._apply_aec(frame)
                if self._on_frame is not None:
                    await _maybe_await(self._on_frame(frame, cleaned))
                if self._vad is not None:
                    await self._dispatch_vad(frame, cleaned)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.capture_errors += 1
            logger.exception("audio_capture_failed")

    def _apply_aec(self, frame: AudioFrame) -> np.ndarray:
        pcm = pcm16_to_float(frame.data, frame.channels)
        if pcm.ndim > 1:
            pcm = pcm.mean(axis=1)
        reference = self._reference.last(pcm.size, self._reference_delay_samples)
        return self._aec.process(pcm, reference)

    async def _dispatch_vad(self, frame: AudioFrame, cleaned: np.ndarray) -> None:
        vad_frame = AudioFrame(
            data=float_to_pcm16(cleaned),
            sample_rate=frame.sample_rate,
            channels=1,
            ts_device=frame.ts_device,
            seq=frame.seq,
        )
        event = self._vad.process(vad_frame) if self._vad is not None else None
        if event is None:
            return
        self.vad_events += 1
        if self._on_vad is not None:
            await _maybe_await(self._on_vad(event))

    async def _playback_loop(self) -> None:
        while self._running:
            if not self._pending:
                self._wakeup.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wakeup.wait(), timeout=0.1)
                continue

            chunk = self._pending.popleft()
            self._pending_bytes -= len(chunk.data)
            self._playing = True
            try:
                await self._transport.play(chunk)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("playback_failed")
                self.dropped_chunks += 1
                continue
            finally:
                self._playing = False

            self.played_chunks += 1
            pcm = pcm16_to_float(chunk.data, chunk.channels)
            if pcm.ndim > 1:
                pcm = pcm.mean(axis=1)
            if chunk.sample_rate != self._sample_rate:
                pcm = resample_float(pcm, chunk.sample_rate, self._sample_rate)
            self._reference.append(pcm)


async def _maybe_await(result: object) -> None:
    """回调既可为同步函数，也可为协程函数（编排层的采集/VAD 回调是异步的）。"""
    if inspect.isawaitable(result):
        await result
