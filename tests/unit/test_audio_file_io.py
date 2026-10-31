"""T-1.4 文件通道单测：WAV 采集切帧、播放录制、flush 计数、错误路径。"""

from __future__ import annotations

import wave

import numpy as np
import pytest

from robotchat.audio.buffer import float_to_pcm16, pcm16_to_float
from robotchat.audio.file_io import FileAudioTransport
from robotchat.audio.frame import AudioChunk

RATE = 16000


def _write_wav(
    path, samples: np.ndarray, rate: int = RATE, channels: int = 1, sampwidth: int = 2
) -> None:
    payload = float_to_pcm16(samples) if sampwidth == 2 else bytes(len(samples))
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sampwidth)
        writer.setframerate(rate)
        writer.writeframes(payload)


async def test_capture_splits_wav_into_frames(tmp_path) -> None:
    path = tmp_path / "in.wav"
    _write_wav(path, np.zeros(RATE // 10, dtype=np.float32))
    transport = FileAudioTransport({"capture_path": str(path), "frame_ms": 20})

    await transport.open()
    frames = [frame async for frame in transport.capture()]
    await transport.close()

    assert [frame.seq for frame in frames] == [0, 1, 2, 3, 4]
    assert all(len(frame.data) == 320 * 2 for frame in frames)
    assert all(frame.sample_rate == RATE for frame in frames)


async def test_playback_is_recorded_to_wav(tmp_path) -> None:
    in_path = tmp_path / "in.wav"
    out_path = tmp_path / "out.wav"
    _write_wav(in_path, np.zeros(320, dtype=np.float32))
    transport = FileAudioTransport(
        {"capture_path": str(in_path), "playback_path": str(out_path)}
    )

    tone = 0.5 * np.sin(2 * np.pi * 1000 * np.arange(320) / RATE)
    await transport.open()
    await transport.play(AudioChunk(data=float_to_pcm16(tone), sample_rate=RATE))
    await transport.flush()
    await transport.close()

    assert transport.played_chunks == 1
    assert transport.flush_count == 1

    with wave.open(str(out_path), "rb") as reader:
        recorded = pcm16_to_float(reader.readframes(reader.getnframes()))
    assert np.allclose(recorded, tone, atol=2e-3)


async def test_missing_capture_file_raises(tmp_path) -> None:
    transport = FileAudioTransport({"capture_path": str(tmp_path / "missing.wav")})
    with pytest.raises(FileNotFoundError):
        await transport.open()


async def test_rejects_non_16bit_wav(tmp_path) -> None:
    path = tmp_path / "in8.wav"
    _write_wav(path, np.zeros(320, dtype=np.float32), sampwidth=1)
    transport = FileAudioTransport({"capture_path": str(path)})
    with pytest.raises(ValueError):
        await transport.open()
