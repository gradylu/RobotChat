"""音频诊断（T-1.9）与唤醒词诊断（T-3A.8）。

- `probe`：验证采集链路在指定时长内持续出帧、帧率是否达标、电平是否合理（C-118）
- `loopback`：播放已知 1kHz 参考音并同步采集，用归一化互相关判断播放是否真的出声（C-119）
- `run_wake_test`：把一段包含唤醒词的 WAV 喂给 KWS 驱动，验证唤醒链路（C-140）
- `check_wakeword`：启动自检第 13 项，校验唤醒词资产与常开聆听 ASR 本地性（C-147 / C-148）
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from robotchat.audio.adapters import create_device_adapter
from robotchat.audio.buffer import float_to_pcm16, pcm16_to_float, rms_dbfs
from robotchat.audio.device import AudioDeviceAdapter
from robotchat.audio.frame import AudioChunk, AudioFrame
from robotchat.audio.pipeline import AudioPipeline
from robotchat.audio.profile import (
    ProfileError,
    list_profiles,
    resolve_profile,
)
from robotchat.audio.transport import AudioTransport
from robotchat.config.schema import DeviceProfileSchema, Settings


@dataclass(frozen=True, slots=True)
class AudioProbeResult:
    frames: int
    duration_s: float
    frames_per_second: float
    rms_dbfs: float
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LoopbackResult:
    played_chunks: int
    captured_frames: int
    correlation: float
    lag_ms: float
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


async def probe(
    transport: AudioTransport,
    duration_s: float = 3.0,
    sample_rate: int = 16000,
) -> AudioProbeResult:
    """采集活性探测：`duration_s` 内应持续出帧且帧率与采样率匹配。"""
    await transport.open()
    started = time.monotonic()
    frames = 0
    frame_seconds = 0.0
    collected: list[np.ndarray] = []
    try:
        async for frame in transport.capture():
            frames += 1
            if frames == 1:
                samples = len(frame.data) / (2 * max(1, frame.channels))
                frame_seconds = samples / max(1, frame.sample_rate)
            collected.append(pcm16_to_float(frame.data, frame.channels))
            if time.monotonic() - started >= duration_s:
                break
    finally:
        await transport.close()

    elapsed = max(1e-6, time.monotonic() - started)
    audio = np.concatenate(collected) if collected else np.zeros(0, dtype=np.float32)
    fps = frames / elapsed
    level = rms_dbfs(audio)
    expected_fps = 1.0 / frame_seconds if frame_seconds > 0 else 50.0
    # 帧率容差 50%：只用于识别「链路停滞」，不追求精确节拍（调度抖动不应误报）
    ok = frames > 0 and fps >= expected_fps * 0.5
    detail = (
        "采集正常"
        if ok
        else "未收到音频或帧率明显低于预期，请检查 DDS 发现 / 话题配置（deployment §8.2）"
    )
    return AudioProbeResult(
        frames=frames,
        duration_s=round(elapsed, 3),
        frames_per_second=round(fps, 2),
        rms_dbfs=round(level, 2),
        ok=ok,
        detail=detail,
    )


def _tone_chunks(
    duration_s: float, sample_rate: int, frame_ms: int, freq_hz: float, amplitude: float
) -> list[AudioChunk]:
    frame_samples = int(sample_rate * frame_ms / 1000)
    total = int(sample_rate * duration_s)
    t = np.arange(total, dtype=np.float64) / sample_rate
    signal = amplitude * np.sin(2 * np.pi * freq_hz * t)
    chunks: list[AudioChunk] = []
    for start in range(0, total, frame_samples):
        block = signal[start : start + frame_samples]
        chunks.append(
            AudioChunk(data=float_to_pcm16(block), sample_rate=sample_rate, channels=1)
        )
    return chunks


def _normalized_correlation(
    reference: np.ndarray, recorded: np.ndarray, max_lag: int
) -> tuple[float, int]:
    """参考与录制的归一化互相关峰值（限制滞后范围，避免远端噪声拉高分数）。"""
    if reference.size == 0 or recorded.size == 0:
        return 0.0, 0
    size = reference.size + recorded.size
    fft_size = 1 << (size - 1).bit_length()
    ref_spec = np.fft.rfft(reference, fft_size)
    rec_spec = np.fft.rfft(recorded, fft_size)
    cross = np.fft.irfft(np.conj(ref_spec) * rec_spec, fft_size)
    lags = np.arange(0, min(max_lag, recorded.size))
    peaks = cross[lags]
    index = int(np.argmax(peaks))
    energy = float(np.linalg.norm(reference) * np.linalg.norm(recorded))
    return (float(peaks[index]) / energy if energy else 0.0), int(lags[index])


async def loopback(
    transport: AudioTransport,
    duration_s: float = 2.0,
    sample_rate: int = 16000,
    frame_ms: int = 20,
    tone_hz: float = 1000.0,
) -> LoopbackResult:
    """回环测试：边播边采，用互相关判断扬声器是否真的出声（现场需物理回环）。"""
    captured: list[np.ndarray] = []

    def on_frame(frame: object, cleaned: np.ndarray) -> None:
        captured.append(np.asarray(cleaned, dtype=np.float32).copy())

    pipeline = AudioPipeline(
        transport,
        on_frame=on_frame,
        sample_rate=sample_rate,
        frame_ms=frame_ms,
    )
    chunks = _tone_chunks(duration_s, sample_rate, frame_ms, tone_hz, amplitude=0.3)

    await pipeline.start()
    try:
        for chunk in chunks:
            await pipeline.play(chunk)
            await asyncio.sleep(frame_ms / 1000.0)
        await asyncio.sleep(0.2)
    finally:
        played = pipeline.played_chunks
        await pipeline.stop()

    reference = np.concatenate(
        [pcm16_to_float(chunk.data, 1) for chunk in chunks]
    ).astype(np.float32)
    recorded = (
        np.concatenate(captured).astype(np.float32)
        if captured
        else np.zeros(0, dtype=np.float32)
    )
    correlation, lag = _normalized_correlation(
        reference, recorded, max_lag=int(sample_rate * 0.2)
    )
    ok = correlation >= 0.5
    detail = (
        "回环链路正常"
        if ok
        else "未检测到参考音（检查扬声器 / 回环接线 / 播放话题；无物理回环时该测试不适用）"
    )
    return LoopbackResult(
        played_chunks=played,
        captured_frames=len(captured),
        correlation=round(correlation, 3),
        lag_ms=round(lag * 1000.0 / sample_rate, 1),
        ok=ok,
        detail=detail,
    )


# ---------------------------------------------------------------- 唤醒词（T-3A.8）


@dataclass(frozen=True, slots=True)
class WakeTestResult:
    detected: bool
    phrase: str
    confidence: float
    frames: int
    duration_s: float
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SelfCheckItem:
    """自检单项结论（PASS / WARN / FAIL）。"""

    index: int
    key: str
    name: str
    status: str
    detail: str
    hints: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["hints"] = list(self.hints)
        return data


SELFCHECK_WAKEWORD_INDEX = 13


def _read_wav_frames(path: Path, frame_ms: int) -> tuple[list[AudioFrame], int]:
    if not path.exists():
        raise FileNotFoundError(f"唤醒词测试音频不存在：{path}")
    with wave.open(str(path), "rb") as reader:
        if reader.getsampwidth() != 2:
            raise ValueError(f"仅支持 16bit WAV：{path}")
        sample_rate = reader.getframerate()
        channels = reader.getnchannels()
        samples_per_frame = max(1, int(sample_rate * frame_ms / 1000))
        frames: list[AudioFrame] = []
        seq = 0
        while True:
            data = reader.readframes(samples_per_frame)
            if not data:
                break
            frames.append(
                AudioFrame(
                    data=data,
                    sample_rate=sample_rate,
                    channels=channels,
                    seq=seq,
                    ts_device=seq * frame_ms / 1000.0,
                )
            )
            seq += 1
    return frames, sample_rate


async def run_wake_test(
    provider: Any,
    wav_path: str | Path,
    *,
    frame_ms: int = 20,
    timeout_s: float = 5.0,
) -> WakeTestResult:
    """把样音频逐帧喂给 KWS 驱动，验证「音频 → score → 唤醒事件」链路（C-140）。"""
    path = Path(wav_path)
    frames, _ = _read_wav_frames(path, frame_ms)
    await provider.warmup()
    stream = await provider.open_stream(None)
    events: list[Any] = []

    async def consume() -> None:
        async for event in provider.events():
            events.append(event)

    consumer = asyncio.create_task(consume(), name="wake-test-consumer")
    # 让消费任务先拿到事件队列再喂帧，否则全部喂完就关闭会让事件无人接收
    await asyncio.sleep(0)
    started = time.monotonic()
    try:
        for frame in frames:
            await stream.feed(frame)
            if events or time.monotonic() - started >= timeout_s:
                break
    finally:
        await stream.close()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(consumer, timeout=1.0)
        await provider.aclose()

    elapsed = round(time.monotonic() - started, 3)
    detected = bool(events)
    first = events[0] if events else None
    detail = (
        f"检测到唤醒词「{getattr(first, 'phrase', '')}」"
        if detected
        else "未检测到唤醒词：请核对样音频、模型文件与阈值配置"
    )
    return WakeTestResult(
        detected=detected,
        phrase=str(getattr(first, "phrase", "")) if first is not None else "",
        confidence=round(float(getattr(first, "confidence", 0.0)), 4)
        if first is not None
        else 0.0,
        frames=len(frames),
        duration_s=elapsed,
        ok=detected,
        detail=detail,
    )


def check_wakeword(
    settings: Settings, *, wake_state: dict[str, Any] | None = None
) -> SelfCheckItem:
    """启动自检第 13 项：唤醒词资产 + 常开聆听 ASR 本地性（FR-14.8 / C-147）。"""
    wakeword = settings.providers.wakeword
    asr = settings.providers.asr
    asr_cloud = bool(asr.drivers.get(asr.active, {}).get("cloud", False))

    def item(status: str, detail: str, hints: tuple[str, ...] = ()) -> SelfCheckItem:
        return SelfCheckItem(
            index=SELFCHECK_WAKEWORD_INDEX,
            key="wakeword",
            name="唤醒词 / 常开聆听",
            status=status,
            detail=detail,
            hints=hints,
        )

    if not wakeword.enabled:
        if asr_cloud and not wakeword.open_ear.cloud_asr_ack:
            return item(
                "FAIL",
                f"关闭唤醒词但 ASR 为云端驱动（{asr.active}）且未确认持续上传",
                (
                    "置 providers.wakeword.open_ear.cloud_asr_ack=true 明确接受上传",
                    "或改用本地 ASR 驱动",
                ),
            )
        if asr_cloud:
            return item(
                "WARN",
                "常开聆听启用云端 ASR：唤醒持续生效，音频将持续上传至云端",
                ("如非必要请改用本地 ASR；上传时长见指标 open_ear_cloud_audio_ms",),
            )
        return item(
            "PASS",
            f"常开聆听 + 本地 ASR（{asr.active}），不经过唤醒词，音频不出本机",
        )

    config = wakeword.drivers.get(wakeword.active, {})
    paths = [str(path) for path in config.get("model_paths") or []]
    missing = [path for path in paths if not Path(path).exists()]
    frontend = [
        str(config.get("melspec_model_path") or "assets/wakeword/melspectrogram.onnx"),
        str(config.get("embedding_model_path") or "assets/wakeword/embedding_model.onnx"),
    ]
    frontend_missing = [path for path in frontend if not Path(path).exists()]
    threshold = float(config.get("threshold", 0.6))
    threshold_bad = not 0.0 < threshold <= 1.0
    hints: list[str] = []
    if not paths:
        hints.append("在 providers.wakeword.drivers 中配置 model_paths（中英双模型各一）")
    if frontend_missing:
        hints.append("缺失的推理前端模型：" + ", ".join(frontend_missing))
    if missing:
        hints.append("缺失的唤醒词模型：" + ", ".join(missing))
        if len(missing) < len(paths):
            hints.append("部分唤醒词不可用：其余语言请自训后替换（T-3A.2 / FR-14.14）")
    if threshold_bad:
        hints.append(f"threshold={threshold} 超出 (0, 1] 范围")
    state = wake_state or {}
    if state.get("kws_error"):
        hints.append(f"KWS 运行时错误：{state['kws_error']}")
    if hints:
        blocking = (
            not paths
            or threshold_bad
            or bool(frontend_missing)
            or len(missing) == len(paths)
        )
        return item(
            "FAIL" if blocking else "WARN",
            f"唤醒词驱动 {wakeword.active} 配置不完整",
            tuple(hints),
        )
    return item(
        "PASS",
        f"唤醒词驱动 {wakeword.active}：{len(paths)} 个模型，阈值 {threshold}，"
        f"活跃窗口 {wakeword.active_window_ms}ms",
    )


# ---------------------------------------------------------------- 机型 Profile（T-1A.6）


@dataclass(frozen=True, slots=True)
class ProfileTestResult:
    profile: str
    capture_ok: bool
    playback_ok: bool
    flush_ok: bool
    frames: int
    reconnects: int
    flush_ms: float
    timestamp_monotonic: bool
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def profile_report(
    settings: Settings,
    config_dir: Path,
    *,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """`cli audio devices`：列出已注册 Profile 并给出当前机型匹配结论（C-168）。"""
    available = [name for name in list_profiles(config_dir)]
    distro = (environ or {}).get("ROS_DISTRO", "")
    try:
        resolution = resolve_profile(settings.audio, config_dir, environ=environ)
    except ProfileError as exc:
        return {
            "ok": False,
            "ros_distro": distro,
            "available": available,
            "resolved": None,
            "detail": str(exc),
        }
    return {
        "ok": True,
        "ros_distro": distro,
        "available": available,
        "resolved": resolution.as_dict(),
        "detail": (
            f"生效 Profile：{resolution.profile.profile}（{resolution.source}），"
            f"采集 {resolution.profile.capture.topic} @ "
            f"{resolution.profile.capture.sample_rate}Hz，"
            f"播放 {resolution.profile.playback.topic} @ "
            f"{resolution.profile.playback.sample_rate}Hz"
        ),
    }


def _test_tone(duration_ms: int = 200, rate: int = 16000) -> AudioChunk:
    samples = np.zeros(int(rate * duration_ms / 1000), dtype=np.float32)
    tones = int(rate * duration_ms / 1000)
    t = np.arange(tones, dtype=np.float64) / rate
    samples[:tones] = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    return AudioChunk(
        data=float_to_pcm16(samples), sample_rate=rate, channels=1, bit_depth=16
    )


async def profile_test(
    profile: DeviceProfileSchema,
    *,
    adapter: AudioDeviceAdapter | None = None,
    duration_s: float = 1.0,
    flush_budget_ms: float = 100.0,
) -> ProfileTestResult:
    """`cli audio profile-test`：逐项验证采集 / 播放 / flush 与时间戳（C-168）。

    传入 `adapter` 可注入模拟后端，便于无 ROS2 环境的契约测试。
    """
    instance = adapter or create_device_adapter(profile, capture_timeout_s=0.2)
    await instance.open()
    frames: list[AudioFrame] = []
    playback_ok = True
    detail = ""
    try:
        async def collect() -> None:
            deadline = time.monotonic() + duration_s
            async for frame in instance.capture.stream():
                frames.append(frame)
                if time.monotonic() >= deadline:
                    return

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(collect(), timeout=duration_s + 0.5)
        try:
            await instance.playback.play(_test_tone(rate=16000))
        except Exception as exc:
            playback_ok = False
            detail = f"播放失败：{exc!r}"
        flush_started = time.monotonic()
        await instance.playback.flush()
        flush_ms = round((time.monotonic() - flush_started) * 1000.0, 2)
    finally:
        await instance.close()

    capture_ok = bool(frames)
    timestamp_monotonic = all(
        frames[index].ts_device <= frames[index + 1].ts_device
        for index in range(len(frames) - 1)
    )
    seq_ok = all(
        frames[index].seq == frames[index - 1].seq + 1
        for index in range(1, len(frames))
    )
    flush_ok = flush_ms <= flush_budget_ms
    ok = capture_ok and playback_ok and flush_ok and timestamp_monotonic and seq_ok
    if not detail:
        if not capture_ok:
            detail = "未收到采集数据：请核对 Profile 话题是否存在、设备侧是否在发布"
        elif not timestamp_monotonic:
            detail = "采集时间戳出现回退：请检查 Profile timestamp.source / unit"
        elif not seq_ok:
            detail = "帧序号不连续：存在丢帧"
        elif not flush_ok:
            detail = f"flush 用时 {flush_ms}ms，超出 {flush_budget_ms}ms 预算"
        else:
            detail = (
                f"采集 {len(frames)} 帧，播放与 flush（{flush_ms}ms）正常；"
                f"Profile {profile.profile} 与现场接口匹配"
            )
    return ProfileTestResult(
        profile=profile.profile,
        capture_ok=capture_ok,
        playback_ok=playback_ok,
        flush_ok=flush_ok,
        frames=len(frames),
        reconnects=getattr(instance, "reconnects", 0),
        flush_ms=flush_ms,
        timestamp_monotonic=timestamp_monotonic,
        ok=ok,
        detail=detail,
    )
