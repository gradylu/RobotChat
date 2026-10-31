"""本地文件回放 / 录制适配器（T-1.4）：不接 ROS2 也能跑通完整链路（C-92、C-123）。

- `capture_path`：采集回放用的 WAV 文件（16bit）
- `playback_path`：播放录制输出，留空则丢弃（只计数）
- `realtime`：按帧时长节拍输出，模拟真实采集；诊断与单测默认全速
"""

from __future__ import annotations

import asyncio
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from robotchat.audio.frame import AudioChunk, AudioFrame
from robotchat.providers.registry import register_provider


@register_provider("transport", "file")
class FileAudioTransport:
    """文件通道：开发、单测与现场诊断的自测路径（不依赖 ROS2）。"""

    name = "file"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.capture_path = Path(str(cfg.get("capture_path") or "var/audio_in.wav"))
        playback = cfg.get("playback_path")
        self.playback_path = Path(str(playback)) if playback else None
        self.frame_ms = int(cfg.get("frame_ms", 20))
        self.realtime = bool(cfg.get("realtime", False))

        self.sample_rate = 16000
        self.channels = 1
        self.playback_sample_rate = 16000
        self.playback_channels = 1
        self.played_chunks = 0
        self.flush_count = 0
        self.is_open = False

        self._frames: list[AudioFrame] = []
        self._writer: wave.Wave_write | None = None

    async def open(self) -> None:
        if not self.capture_path.exists():
            raise FileNotFoundError(f"采集音频文件不存在：{self.capture_path}")

        self._frames = self._read_wav(self.capture_path)

        if self.playback_path is not None:
            self.playback_path.parent.mkdir(parents=True, exist_ok=True)
            # 录制期间保持写句柄打开（close() 落盘），不能放进 with 块
            writer = wave.open(str(self.playback_path), "wb")  # noqa: SIM115
            writer.setnchannels(self.playback_channels)
            writer.setsampwidth(2)
            writer.setframerate(self.playback_sample_rate)
            self._writer = writer

        self.is_open = True

    def _read_wav(self, path: Path) -> list[AudioFrame]:
        with wave.open(str(path), "rb") as reader:
            if reader.getsampwidth() != 2:
                raise ValueError(f"仅支持 16bit WAV：{path}（实际 {reader.getsampwidth() * 8}bit）")
            self.sample_rate = reader.getframerate()
            self.channels = reader.getnchannels()
            self.playback_sample_rate = self.sample_rate
            self.playback_channels = self.channels
            samples_per_frame = max(1, int(self.sample_rate * self.frame_ms / 1000))
            frames: list[AudioFrame] = []
            seq = 0
            while True:
                data = reader.readframes(samples_per_frame)
                if not data:
                    break
                frames.append(
                    AudioFrame(
                        data=data,
                        sample_rate=self.sample_rate,
                        channels=self.channels,
                        seq=seq,
                        ts_device=seq * self.frame_ms / 1000.0,
                    )
                )
                seq += 1
        return frames

    def capture(self) -> AsyncIterator[AudioFrame]:
        async def generator() -> AsyncIterator[AudioFrame]:
            for frame in self._frames:
                if self.realtime:
                    await asyncio.sleep(self.frame_ms / 1000.0)
                yield frame

        return generator()

    async def play(self, chunk: AudioChunk) -> None:
        if self._writer is not None:
            self._writer.writeframesraw(chunk.data)
        self.played_chunks += 1

    async def flush(self) -> None:
        # 文件通道没有物理扬声器；语义上等价于「后续待播音频不再输出」，
        # 由调用方（AudioPipeline）清空队列，这里只记录次数供诊断核对。
        self.flush_count += 1

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        self.is_open = False
