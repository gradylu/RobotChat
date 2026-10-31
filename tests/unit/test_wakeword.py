"""Phase 3A 唤醒词单测：openWakeWord 驱动、待机调度、指标计数、诊断与自检（T-3A.1 ~ T-3A.8）。"""

from __future__ import annotations

import asyncio
import json
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from robotchat.audio.frame import AudioFrame
from robotchat.audio.kws import KwsScheduler
from robotchat.config.schema import build_settings
from robotchat.health import HealthStatus
from robotchat.observability.diagnostics import (
    SELFCHECK_WAKEWORD_INDEX,
    check_wakeword,
    run_wake_test,
)
from robotchat.observability.metrics import (
    OPEN_EAR_CLOUD_AUDIO,
    WAKE_COUNT,
    MetricsCollector,
)
from robotchat.providers.base import KwsCapabilities, WakeEvent
from robotchat.providers.kws.openwakeword import OpenWakeWordKws
from robotchat.providers.registry import ProviderRegistry

RATE = 16000
FRAME_MS = 80
FRAME_SAMPLES = RATE * FRAME_MS // 1000


class ScriptedInference:
    """按调用顺序返回预设 score，不需要真实 ONNX 模型。"""

    def __init__(self, scores: list[dict[str, float]]) -> None:
        self.scores = scores
        self.calls = 0

    def __call__(self, pcm: np.ndarray) -> dict[str, float]:
        del pcm
        index = min(self.calls, len(self.scores) - 1)
        self.calls += 1
        return dict(self.scores[index])


class FakeKwsStream:
    def __init__(self, provider: FakeKwsProvider) -> None:
        self.provider = provider
        self.frames = 0
        self.closed = False

    async def feed(self, frame: AudioFrame) -> None:
        del frame
        self.frames += 1

    async def close(self) -> None:
        self.closed = True
        self.provider.queue.put_nowait(None)


class FakeKwsProvider:
    name = "fake_kws"
    capabilities = KwsCapabilities()

    def __init__(self) -> None:
        self.queue: asyncio.Queue[WakeEvent | None] = asyncio.Queue()
        self.stream: FakeKwsStream | None = None
        self.warmed = False
        self.opened = 0
        self.closed = False

    async def warmup(self) -> None:
        self.warmed = True

    async def open_stream(self, cancel: Any = None) -> FakeKwsStream:
        del cancel
        self.queue = asyncio.Queue()
        self.stream = FakeKwsStream(self)
        self.opened += 1
        return self.stream

    def events(self) -> AsyncIterator[WakeEvent]:
        async def generator() -> AsyncIterator[WakeEvent]:
            while True:
                event = await self.queue.get()
                if event is None:
                    return
                yield event

        return generator()

    async def health(self) -> HealthStatus:
        return HealthStatus.ok("fake")

    async def aclose(self) -> None:
        self.closed = True


def _frame(samples: int = FRAME_SAMPLES) -> AudioFrame:
    return AudioFrame(data=b"\x00\x00" * samples, sample_rate=RATE)


async def _collect(provider: Any, count: int, *, timeout: float = 1.0) -> list[WakeEvent]:
    events: list[WakeEvent] = []

    async def consume() -> None:
        async for event in provider.events():
            events.append(event)
            if len(events) >= count:
                return

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.TimeoutError:
        task.cancel()
    return events


# ---------------------------------------------------------------- openWakeWord 驱动


async def test_threshold_and_cooldown_gate_detections() -> None:
    provider = OpenWakeWordKws(
        {"wake_phrases": ["你好机器人"], "threshold": 0.6, "cooldown_ms": 2000},
        inference=ScriptedInference(
            [{"你好机器人": 0.2}, {"你好机器人": 0.9}, {"你好机器人": 0.95}]
        ),
        clock=lambda: 0.0,
    )
    stream = await provider.open_stream(None)
    consumer = asyncio.create_task(_collect(provider, 1, timeout=0.2))
    for _ in range(3):
        await stream.feed(_frame())
        await asyncio.sleep(0)
    events = await consumer
    await stream.close()

    # 首帧低于阈值；第二帧命中；第三帧被冷却窗拦住
    assert len(events) == 1
    assert events[0].phrase == "你好机器人"
    assert events[0].confidence == pytest.approx(0.9)
    assert provider.detections == 1
    assert provider.peak_scores["你好机器人"] == pytest.approx(0.95)


async def test_confirm_twice_requires_two_consecutive_frames() -> None:
    provider = OpenWakeWordKws(
        {"wake_phrases": ["小智"], "threshold": 0.6, "confirm_twice": True},
        inference=ScriptedInference(
            [{"小智": 0.9}, {"小智": 0.9}, {"小智": 0.9}, {"小智": 0.9}]
        ),
        clock=lambda: 0.0,
    )
    stream = await provider.open_stream(None)
    consumer = asyncio.create_task(_collect(provider, 1, timeout=0.2))
    for _ in range(4):
        await stream.feed(_frame())
        await asyncio.sleep(0)
    events = await consumer
    await stream.close()

    # 单帧命中不触发，第二帧连续命中才产生一次唤醒
    assert len(events) == 1
    assert provider.frames_processed == 4


async def test_multi_phrase_picks_highest_confidence() -> None:
    provider = OpenWakeWordKws(
        {"wake_phrases": ["你好机器人", "hello robot"], "threshold": 0.6},
        inference=ScriptedInference(
            [
                {"你好机器人": 0.0, "hello robot": 0.0},
                {"你好机器人": 0.68, "hello robot": 0.82},
            ]
        ),
        clock=lambda: 0.0,
    )
    stream = await provider.open_stream(None)
    consumer = asyncio.create_task(_collect(provider, 1, timeout=0.2))
    for _ in range(2):
        await stream.feed(_frame())
        await asyncio.sleep(0)
    events = await consumer
    await stream.close()

    assert [event.phrase for event in events] == ["hello robot"]


async def test_frames_are_aligned_to_declared_frame_ms() -> None:
    provider = OpenWakeWordKws(
        {"wake_phrases": ["小智"], "threshold": 2.0},
        inference=ScriptedInference([{"小智": 0.0}]),
    )
    stream = await provider.open_stream(None)
    # 20ms 采集帧 → 4 帧拼成 1 个 80ms 推理块
    small = _frame(samples=RATE * 20 // 1000)
    for _ in range(8):
        await stream.feed(small)
    await stream.close()

    assert provider.frames_processed == 2


# ---------------------------------------------------------------- onnxruntime 后端


ASSETS = Path(__file__).resolve().parents[2] / "assets" / "wakeword"


def _asset(name: str) -> str:
    path = ASSETS / name
    if not path.exists():
        pytest.skip(f"唤醒词 ONNX 资产未入库：{name}")
    return str(path)


def test_onnx_backend_scores_one_chunk() -> None:
    from robotchat.providers.kws.oww_onnx import OpenWakeWordOnnx

    backend = OpenWakeWordOnnx(
        [_asset("hey_jarvis_v0.1.onnx")],
        _asset("melspectrogram.onnx"),
        _asset("embedding_model.onnx"),
    )
    scores = backend(np.zeros(FRAME_SAMPLES, dtype=np.float32))

    assert set(scores) == {"hey_jarvis_v0.1"}
    assert 0.0 <= scores["hey_jarvis_v0.1"] <= 1.0


def test_onnx_backend_accumulates_partial_chunk() -> None:
    from robotchat.providers.kws.oww_onnx import OpenWakeWordOnnx

    backend = OpenWakeWordOnnx(
        [_asset("hey_jarvis_v0.1.onnx")],
        _asset("melspectrogram.onnx"),
        _asset("embedding_model.onnx"),
    )
    # 不足一个 80ms 分块时不应触发推理，返回上一次分数
    scores = backend(np.zeros(FRAME_SAMPLES // 2, dtype=np.float32))
    assert scores == {"hey_jarvis_v0.1": 0.0}

    backend(np.zeros(FRAME_SAMPLES // 2, dtype=np.float32))
    assert backend.model_names == ["hey_jarvis_v0.1"]


async def test_driver_loads_real_onnx_assets() -> None:
    provider = OpenWakeWordKws(
        {
            "model_paths": [_asset("hey_jarvis_v0.1.onnx")],
            "melspec_model_path": _asset("melspectrogram.onnx"),
            "embedding_model_path": _asset("embedding_model.onnx"),
            "wake_phrases": ["hey jarvis"],
            "threshold": 0.6,
        }
    )
    before = await provider.health()
    assert before.state == "degraded"  # 文件齐全但尚未加载

    await provider.warmup()
    assert (await provider.health()).state == "ok"

    stream = await provider.open_stream(None)
    for _ in range(3):
        await stream.feed(_frame())
    await stream.close()

    assert provider.frames_processed == 3
    assert provider.last_scores  # 静音输入也应产出分数
    await provider.aclose()


# ---------------------------------------------------------------- 待机调度


async def test_scheduler_feeds_only_in_standby() -> None:
    provider = FakeKwsProvider()
    standby = {"value": True}
    scheduler = KwsScheduler(
        provider, on_wake=lambda event: None, is_standby=lambda: standby["value"]
    )

    await scheduler.start()
    assert scheduler.running is True
    await scheduler.feed(_frame())
    standby["value"] = False
    await scheduler.feed(_frame())
    await scheduler.feed(_frame())
    await scheduler.stop()

    assert scheduler.frames_fed == 1
    assert scheduler.frames_skipped == 2
    assert scheduler.running is False


async def test_scheduler_counts_wake_and_dispatches_handler() -> None:
    provider = FakeKwsProvider()
    received: list[WakeEvent] = []
    metrics = MetricsCollector()

    async def on_wake(event: WakeEvent) -> None:
        received.append(event)

    scheduler = KwsScheduler(provider, on_wake=on_wake, metrics=metrics)
    await scheduler.start()
    assert provider.stream is not None
    provider.queue.put_nowait(WakeEvent(type="detected", phrase="小智", confidence=0.9))
    await asyncio.sleep(0.05)
    await scheduler.stop()

    assert received and received[0].phrase == "小智"
    assert metrics.counter(WAKE_COUNT) == 1
    assert scheduler.wake_events == 1


# ---------------------------------------------------------------- 指标


def test_counters_track_wake_metrics() -> None:
    metrics = MetricsCollector()
    metrics.increment(WAKE_COUNT)
    metrics.increment(WAKE_COUNT, 2)
    metrics.increment(OPEN_EAR_CLOUD_AUDIO, 40)

    assert metrics.counter(WAKE_COUNT) == 3
    assert metrics.counters() == {WAKE_COUNT: 3, OPEN_EAR_CLOUD_AUDIO: 40}
    metrics.reset()
    assert metrics.counters() == {}
    assert metrics.counter(WAKE_COUNT) == 0


# ---------------------------------------------------------------- 诊断与自检


def _minimal_raw(
    model_paths: list[str],
    *,
    enabled: bool = True,
    asr_active: str = "local",
    cloud_asr_ack: bool = False,
    frontend: tuple[str, str] | None = None,
) -> dict[str, Any]:
    driver: dict[str, Any] = {"threshold": 0.6, "model_paths": model_paths}
    if frontend is not None:
        driver["melspec_model_path"] = frontend[0]
        driver["embedding_model_path"] = frontend[1]
    asr_drivers = {
        "local": {"cloud": False},
        "cloud": {"cloud": True},
    }
    return {
        "app": {"name": "robotchat", "env": "dev"},
        "audio": {
            "transport": "file",
            "device_profile": "generic_ros_audio",
            "capture": {"target_sample_rate": 16000},
            "playback": {"queue_ms": 300, "flush_budget_ms": 100},
            "aec": {"mode": "auto"},
        },
        "providers": {
            "asr": {"active": asr_active, "drivers": asr_drivers},
            "llm": {"active": "mock", "drivers": {"mock": {"base_url": ""}}},
            "tts": {"active": "mock", "drivers": {"mock": {"voice": "default"}}},
            "knowledge": {"active": "none", "drivers": {}},
            "wakeword": {
                "enabled": enabled,
                "active": "mock",
                "drivers": {"mock": driver},
                "open_ear": {"cloud_asr_ack": cloud_asr_ack},
            },
        },
        "tools": {"policy": {"confirm_timeout_ms": 10000}, "definitions": {}},
    }


def _touch(path: Path) -> Path:
    path.write_bytes(b"0")
    return path


def _write_wav(path: Path, duration_s: float = 0.2, rate: int = 16000) -> None:
    total = int(rate * duration_s)
    signal = (np.zeros(total, dtype=np.int16)).tobytes()
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(signal)


async def test_run_wake_test_detects_from_wav(tmp_path: Path) -> None:
    wav = tmp_path / "wake.wav"
    _write_wav(wav)
    provider = OpenWakeWordKws(
        {"wake_phrases": ["你好机器人"], "threshold": 0.5},
        inference=ScriptedInference([{"你好机器人": 0.95}]),
    )

    result = await run_wake_test(provider, wav)

    assert result.ok is True
    assert result.detected is True
    assert result.phrase == "你好机器人"
    assert result.frames > 0


async def test_run_wake_test_missing_wav(tmp_path: Path) -> None:
    provider = OpenWakeWordKws({"wake_phrases": ["小智"]}, inference=ScriptedInference([{}]))
    with pytest.raises(FileNotFoundError):
        await run_wake_test(provider, tmp_path / "missing.wav")


def test_check_wakeword_flags_missing_models() -> None:
    settings = build_settings(_minimal_raw(["assets/wakeword/missing.onnx"]))
    item = check_wakeword(settings)

    assert item.index == SELFCHECK_WAKEWORD_INDEX
    assert item.status == "FAIL"
    assert "missing.onnx" in " ".join(item.hints)


def test_check_wakeword_passes_with_existing_model(tmp_path: Path) -> None:
    model = tmp_path / "wake.onnx"
    model.write_bytes(b"0")
    frontend = (
        str(_touch(tmp_path / "melspectrogram.onnx")),
        str(_touch(tmp_path / "embedding_model.onnx")),
    )
    settings = build_settings(_minimal_raw([str(model)], frontend=frontend))

    item = check_wakeword(settings)

    assert item.status == "PASS"


def test_check_wakeword_partial_models_missing_is_warn(tmp_path: Path) -> None:
    frontend = (
        str(_touch(tmp_path / "melspectrogram.onnx")),
        str(_touch(tmp_path / "embedding_model.onnx")),
    )
    existing = str(_touch(tmp_path / "hey_jarvis_v0.1.onnx"))
    missing = str(tmp_path / "wake_robot_v1.onnx")
    settings = build_settings(_minimal_raw([existing, missing], frontend=frontend))

    item = check_wakeword(settings)

    # 英文模型可用、中文模型缺失：降级 WARN，服务仍可启动（README §缺失语义）
    assert item.status == "WARN"
    hints = " ".join(item.hints)
    assert "wake_robot_v1.onnx" in hints
    assert "部分唤醒词不可用" in hints


def test_check_wakeword_missing_frontend_fails(tmp_path: Path) -> None:
    model = str(_touch(tmp_path / "wake.onnx"))
    frontend = (str(tmp_path / "melspectrogram.onnx"), str(tmp_path / "embedding_model.onnx"))
    settings = build_settings(_minimal_raw([model], frontend=frontend))

    item = check_wakeword(settings)

    # 前端缺失时所有唤醒词都无法推理，必须阻断
    assert item.status == "FAIL"
    assert "推理前端模型" in " ".join(item.hints)


def test_check_wakeword_open_ear_cloud_requires_ack() -> None:
    # 关闭唤醒词 + 云端 ASR 未置 ack：配置校验会直接阻塞启动，这里直接构造后校验自检结论
    settings = build_settings(
        _minimal_raw([], enabled=False, asr_active="cloud", cloud_asr_ack=True)
    )
    settings.providers.wakeword.open_ear.cloud_asr_ack = False
    assert check_wakeword(settings).status == "FAIL"

    acked = build_settings(
        _minimal_raw([], enabled=False, asr_active="cloud", cloud_asr_ack=True)
    )
    item = check_wakeword(acked)
    assert item.status == "WARN"
    assert "上传" in item.detail


def test_check_wakeword_local_open_ear_passes() -> None:
    settings = build_settings(_minimal_raw([], enabled=False, asr_active="local"))
    assert check_wakeword(settings).status == "PASS"


def test_training_config_aligns_with_runtime_assets() -> None:
    """训练配置的产物路径与 providers.yaml 的 model_paths 必须保持一致（防漂移）。"""
    import yaml

    root = Path(__file__).resolve().parents[2]
    train_cfg = yaml.safe_load(
        (root / "tools" / "wakeword" / "train_zh_wakeword.yaml").read_text(encoding="utf-8")
    )
    providers = yaml.safe_load((root / "config" / "providers.yaml").read_text(encoding="utf-8"))
    driver = providers["providers"]["wakeword"]["drivers"]["openwakeword"]

    output = train_cfg["model"]["output"]
    assert output in driver["model_paths"]
    # model_paths 与 wake_phrases 顺序对齐，命中时才播报正确文案
    index = driver["model_paths"].index(output)
    assert driver["wake_phrases"][index] == train_cfg["model"]["phrase"]


# ---------------------------------------------------------------- CLI


def test_parser_accepts_wake_commands() -> None:
    from robotchat.cli import _build_parser

    test_args = _build_parser().parse_args(["wake", "test", "--input", "a.wav"])
    assert test_args.group == "wake"
    assert test_args.command == "test"
    assert test_args.input == "a.wav"

    status_args = _build_parser().parse_args(["wake", "status"])
    assert status_args.command == "status"


def test_wake_status_get_api() -> None:
    from robotchat.cli import wake_status

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/wake"
        assert request.headers.get("authorization") == "Bearer secret"
        return httpx.Response(200, json={"enabled": True, "state": "IDLE"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = wake_status(url="http://127.0.0.1:8080", token="secret", client=client)
    client.close()

    assert result == {"enabled": True, "state": "IDLE"}


def test_main_wake_status_prints_json(monkeypatch, capsys) -> None:
    from robotchat import cli

    monkeypatch.setattr(cli, "load_drivers", lambda: None)
    monkeypatch.setattr(cli, "configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(cli, "load_settings", lambda **kwargs: object())
    monkeypatch.setattr(cli, "wake_status", lambda **kwargs: {"enabled": True})

    assert cli.main(["wake", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is True


def test_registered_kws_driver() -> None:
    from robotchat.providers import kws  # noqa: F401  导入即注册

    provider = ProviderRegistry().create_active("kws", "openwakeword", {"wake_phrases": ["小智"]})
    assert provider.name == "openwakeword"
