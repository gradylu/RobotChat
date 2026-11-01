"""T-8.2 端到端测试公共桩件：本地文件音频 + 全桩服务，不接硬件、不连网络。

所有用例共享的桩件（音频通道、VAD、ASR、LLM、TTS、工具适配器）、WAV 生成、
settings fixture、轮次等待工具与槽位驱动都集中在这里，测试文件不再依赖
`tests/unit` 下的任何模块。
"""

from __future__ import annotations

import asyncio
import time
import wave
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from robotchat.audio.frame import AudioChunk, AudioFrame
from robotchat.config.loader import load_settings
from robotchat.config.schema import Settings
from robotchat.core.cancel import CancellationToken
from robotchat.core.orchestrator import DialogOrchestrator
from robotchat.dialog.session import SessionStore
from robotchat.health import HealthStatus
from robotchat.observability.metrics import MetricsCollector
from robotchat.providers.base import (
    ASRCapabilities,
    ASREvent,
    ChatRequest,
    LLMCapabilities,
    LLMEvent,
    TTSCapabilities,
    TTSRequest,
    VadEvent,
)
from robotchat.providers.registry import ProviderRegistry, register_provider
from robotchat.tools.base import ToolCall, ToolResult

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
RATE = 16000
FRAME_SAMPLES = 320
SILENCE_FRAME = b"\x00\x00" * FRAME_SAMPLES
LOUD_FRAME = b"\xff\x7f" * FRAME_SAMPLES

BASE_ENVIRON: dict[str, str] = {
    # 全离线：关闭启动公网探测与唤醒词（启动即常开聆听 ACTIVE_IDLE）
    "ROBOTCHAT__RESILIENCE__OFFLINE_PROBE_ON_START": "false",
    "ROBOTCHAT__STATE__HEARTBEAT_MS": "5000",
    "ROBOTCHAT__PROVIDERS__WAKEWORD__ENABLED": "false",
    "ROBOTCHAT__PROVIDERS__ASR__FALLBACK": "",
    "ROBOTCHAT__PROVIDERS__LLM__FALLBACK": "",
    "ROBOTCHAT__PROVIDERS__TTS__FALLBACK": "",
    # 二次确认等待上限缩短，避免用例卡在默认 10s
    "ROBOTCHAT__TOOLS__POLICY__CONFIRM_TIMEOUT_MS": "2000",
}


def make_settings(data_dir: Path, environ: Mapping[str, str] | None = None) -> Settings:
    """加载仓库配置并叠加离线测试约束；`environ` 可覆盖单个用例的额外参数。"""
    merged = dict(BASE_ENVIRON)
    merged.update(dict(environ or {}))
    return load_settings(config_dir=REPO_CONFIG, data_dir=data_dir, environ=merged)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


# ---------------------------------------------------------------- 音频通道


class FakeTransport:
    """内存音频通道：采集帧可指定从第 N 帧起转为高能量（模拟用户插话）。"""

    name = "fake"

    def __init__(
        self,
        frames: int = 400,
        frame_delay: float = 0.01,
        loud_after: int | None = None,
    ) -> None:
        self.frames = frames
        self.frame_delay = frame_delay
        self.loud_after = loud_after
        self.played: list[AudioChunk] = []
        self.flush_count = 0
        self.opened = False
        self.closed = False

    async def open(self) -> None:
        self.opened = True

    def capture(self) -> AsyncIterator[AudioFrame]:
        async def generator() -> AsyncIterator[AudioFrame]:
            for seq in range(self.frames):
                loud = self.loud_after is not None and seq >= self.loud_after
                yield AudioFrame(
                    data=LOUD_FRAME if loud else SILENCE_FRAME,
                    sample_rate=RATE,
                    seq=seq,
                    ts_device=seq * 0.02,
                )
                await asyncio.sleep(self.frame_delay)

        return generator()

    async def play(self, chunk: AudioChunk) -> None:
        self.played.append(chunk)

    async def flush(self) -> None:
        self.flush_count += 1

    async def close(self) -> None:
        self.closed = True


class FakeVad:
    name = "fake_vad"

    def process(self, frame: AudioFrame) -> VadEvent | None:
        del frame
        return None

    def reset(self) -> None:
        return None


class ScriptedVad:
    """按帧序号产出起止音事件；本地 WAV 回放时用于驱动一轮完整对话。"""

    name = "scripted_vad"

    def __init__(self, start_seq: int = 3, end_seq: int = 20, cycle: int | None = None) -> None:
        self.start_seq = start_seq
        self.end_seq = end_seq
        self.cycle = cycle

    def process(self, frame: AudioFrame) -> VadEvent | None:
        seq = frame.seq if self.cycle is None else frame.seq % self.cycle
        if seq == self.start_seq:
            return VadEvent(type="speech_start", energy_db=-18.0)
        if seq == self.end_seq:
            return VadEvent(type="speech_end", energy_db=-40.0)
        return None

    def reset(self) -> None:
        return None


# ---------------------------------------------------------------- ASR 桩件


class FakeASRStream:
    """单条 ASR 流：先吐 partial，关闭后给出 final（可周期重复 partial 供语义确认）。"""

    name = "fake_asr"
    capabilities = ASRCapabilities(
        streaming=True,
        languages=frozenset({"zh", "en", "yue", "dongbei"}),
        sample_rates=(RATE,),
    )

    def __init__(
        self,
        text: str = "",
        *,
        partial: str = "",
        repeat_partials: bool = False,
        partial_interval: float = 0.02,
        final_lang: str = "",
    ) -> None:
        self.text = text
        self.partial = partial
        self.repeat_partials = repeat_partials
        self.partial_interval = partial_interval
        self.final_lang = final_lang
        self.frames = 0
        self.closed = asyncio.Event()

    async def open_stream(
        self, session_id: str, cancel: Any, lang: str = ""
    ) -> FakeASRStream:
        del session_id, cancel, lang
        self.closed.clear()
        return self

    async def feed(self, frame: AudioFrame) -> None:
        del frame
        self.frames += 1

    def events(self) -> AsyncIterator[ASREvent]:
        async def generator() -> AsyncIterator[ASREvent]:
            if self.partial:
                yield ASREvent(type="partial", text=self.partial, lang=self.final_lang)
                while self.repeat_partials and not self.closed.is_set():
                    await asyncio.sleep(self.partial_interval)
                    if self.closed.is_set():
                        break
                    yield ASREvent(type="partial", text=self.partial, lang=self.final_lang)
            await self.closed.wait()
            yield ASREvent(type="final", text=self.text, lang=self.final_lang)

        return generator()

    async def close(self) -> None:
        self.closed.set()


class FakeASRProvider:
    """按打开次数取脚本文本的 ASR 驱动；记录打开次数、语言与累计喂入帧数。"""

    name = "fake_asr"
    capabilities = FakeASRStream.capabilities

    def __init__(
        self,
        texts: list[str] | str,
        *,
        partial: str = "",
        repeat_partials: bool = False,
        partial_interval: float = 0.02,
        final_lang: str = "",
    ) -> None:
        self.texts = [texts] if isinstance(texts, str) else list(texts)
        self.partial = partial
        self.repeat_partials = repeat_partials
        self.partial_interval = partial_interval
        self.final_lang = final_lang
        self.opens = 0
        self.langs: list[str] = []
        self.streams: list[FakeASRStream] = []

    async def open_stream(
        self, session_id: str, cancel: Any, lang: str = ""
    ) -> FakeASRStream:
        del session_id, cancel
        text = self.texts[min(self.opens, len(self.texts) - 1)]
        stream = FakeASRStream(
            text,
            partial=self.partial,
            repeat_partials=self.repeat_partials,
            partial_interval=self.partial_interval,
            final_lang=self.final_lang,
        )
        self.opens += 1
        self.langs.append(lang)
        self.streams.append(stream)
        return stream

    @property
    def total_frames(self) -> int:
        return sum(stream.frames for stream in self.streams)


# ---------------------------------------------------------------- LLM / TTS 桩件


class FakeLLM:
    """顺序产出 token；`gate=True` 时在 done 前挂起，制造「已生成未入队」的尾部文本。"""

    name = "fake_llm"
    capabilities = LLMCapabilities(streaming=True, tool_call=False, context_window=4096)

    def __init__(self, pieces: list[str], *, gate: bool = False) -> None:
        self.pieces = pieces
        self.requests: list[ChatRequest] = []
        self.started = asyncio.Event()
        self._gate = asyncio.Event() if gate else None

    def release(self) -> None:
        if self._gate is not None:
            self._gate.set()

    async def chat_stream(
        self, req: ChatRequest, cancel: Any
    ) -> AsyncIterator[LLMEvent]:
        del cancel
        self.requests.append(req)
        self.started.set()
        for piece in self.pieces:
            await asyncio.sleep(0)
            yield LLMEvent(type="token", text=piece)
        if self._gate is not None:
            await self._gate.wait()
        yield LLMEvent(type="done")


class EchoLLM:
    """按最后一条用户消息原样作答，用于校验多轮 / 连续打断没有串轮。"""

    name = "echo_llm"
    capabilities = LLMCapabilities(streaming=True, tool_call=False, context_window=4096)

    def __init__(self, prefix: str = "答复：", suffix: str = "。") -> None:
        self.prefix = prefix
        self.suffix = suffix
        self.requests: list[ChatRequest] = []

    async def chat_stream(
        self, req: ChatRequest, cancel: Any
    ) -> AsyncIterator[LLMEvent]:
        del cancel
        self.requests.append(req)
        user_text = req.messages[-1].content if req.messages else ""
        yield LLMEvent(type="token", text=f"{self.prefix}{user_text}{self.suffix}")
        yield LLMEvent(type="done")


class ToolCallingLLM:
    """首轮（无工具结果）返回 tool_call，结果回灌后再产出最终文本。"""

    name = "tool_llm"
    capabilities = LLMCapabilities(streaming=True, tool_call=True, context_window=4096)

    def __init__(
        self,
        call: dict[str, Any],
        final: list[str] | None = None,
    ) -> None:
        self.call = call
        self.final = final or ["好的。"]
        self.requests: list[ChatRequest] = []

    async def chat_stream(
        self, req: ChatRequest, cancel: Any
    ) -> AsyncIterator[LLMEvent]:
        del cancel
        self.requests.append(req)
        last = req.messages[-1].content if req.messages else ""
        if not last.startswith("[工具结果]"):
            yield LLMEvent(type="tool_call", tool_call=self.call)
            yield LLMEvent(type="done")
            return
        for piece in self.final:
            await asyncio.sleep(0)
            yield LLMEvent(type="token", text=piece)
        yield LLMEvent(type="done")


class FakeTTS:
    """可配置每句音频块数量与块间隔的 TTS 桩件，记录全部合成请求。"""

    name = "fake_tts"
    capabilities = TTSCapabilities(
        sample_rate=RATE,
        voices=("default", "en_female", "yue_female"),
        languages=frozenset({"zh", "en", "yue"}),
    )

    def __init__(self, chunks: int = 1, interval_s: float = 0.0) -> None:
        self.chunks = chunks
        self.interval_s = interval_s
        self.requests: list[TTSRequest] = []

    async def synthesize_stream(
        self, req: TTSRequest, cancel: Any
    ) -> AsyncIterator[AudioChunk]:
        del cancel
        self.requests.append(req)
        for _ in range(self.chunks):
            await asyncio.sleep(self.interval_s)
            yield AudioChunk(data=SILENCE_FRAME, sample_rate=RATE)


# ---------------------------------------------------------------- 工具适配器


class StubAdapter:
    """最小工具适配器：记录调用并直接返回成功，不依赖 ROS2。"""

    def __init__(self, name: str = "stub", output: str = "执行成功") -> None:
        self.name = name
        self.output = output
        self.calls: list[ToolCall] = []

    async def execute(self, call: ToolCall, cancel: CancellationToken) -> ToolResult:
        del cancel
        self.calls.append(call)
        return ToolResult(ok=True, output=self.output)

    async def health(self) -> HealthStatus:
        return HealthStatus.ok("stub 适配器")


# ---------------------------------------------------------------- 槽位驱动


@register_provider("llm", "e2e_slot_primary")
class SlotPrimaryLLM:
    """槽位主 LLM 驱动：`healthy=False` 时既不可探活也不可用（模拟主链路故障）。"""

    name = "e2e_slot_primary"
    capabilities = LLMCapabilities(streaming=True, tool_call=False, context_window=4096)

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.healthy = bool(cfg.get("healthy", True))
        self.text = str(cfg.get("text", "主驱动回答。"))
        self.requests: list[ChatRequest] = []

    async def health(self) -> HealthStatus:
        if self.healthy:
            return HealthStatus.ok("主驱动可用")
        return HealthStatus.unavailable("主驱动不可用")

    async def chat_stream(
        self, req: ChatRequest, cancel: Any
    ) -> AsyncIterator[LLMEvent]:
        del cancel
        self.requests.append(req)
        if not self.healthy:
            raise RuntimeError("主驱动不可用")
        yield LLMEvent(type="token", text=self.text)
        yield LLMEvent(type="done")


@register_provider("llm", "e2e_slot_fallback")
class SlotFallbackLLM:
    """槽位备用 LLM 驱动。"""

    name = "e2e_slot_fallback"
    capabilities = LLMCapabilities(streaming=True, tool_call=False, context_window=4096)

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.healthy = bool(cfg.get("healthy", True))
        self.text = str(cfg.get("text", "备用驱动回答。"))
        self.requests: list[ChatRequest] = []

    async def health(self) -> HealthStatus:
        return HealthStatus.ok("备用驱动可用")

    async def chat_stream(
        self, req: ChatRequest, cancel: Any
    ) -> AsyncIterator[LLMEvent]:
        del cancel
        self.requests.append(req)
        yield LLMEvent(type="token", text=self.text)
        yield LLMEvent(type="done")


@register_provider("asr", "e2e_slot_asr")
class SlotASR:
    """槽位 ASR 驱动：内部复用 FakeASRProvider，按打开次数取脚本文本。"""

    name = "e2e_slot_asr"
    capabilities = FakeASRStream.capabilities

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        texts = cfg.get("texts") or ["今天天气怎么样？"]
        self.provider = FakeASRProvider(
            texts,
            partial=str(cfg.get("partial", "")),
            repeat_partials=bool(cfg.get("repeat_partials", False)),
            final_lang=str(cfg.get("final_lang", "")),
        )
        self.langs = self.provider.langs

    async def open_stream(
        self, session_id: str, cancel: Any, lang: str = ""
    ) -> FakeASRStream:
        return await self.provider.open_stream(session_id, cancel, lang)


# ---------------------------------------------------------------- WAV 与等待工具


def write_wav(path: Path, frames: int = 30) -> Path:
    """生成 16bit 单声道 16kHz WAV，供 FileAudioTransport 回放。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(SILENCE_FRAME * frames)
    return path


def wav_frames(path: Path) -> int:
    with wave.open(str(path), "rb") as reader:
        return reader.getnframes()


async def wait_until(predicate: Callable[[], Any], timeout: float = 5.0) -> Any:
    """以 5ms 轮询等待条件成立；超时抛断言错误，避免长 sleep。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.005)
    raise AssertionError(f"条件未在 {timeout}s 内满足")


async def wait_turn(
    store: SessionStore,
    seq: int = 1,
    *,
    status: str = "completed",
    session_id: str = "default",
    timeout: float = 5.0,
) -> dict[str, Any]:
    """等待指定轮次达到目标状态并返回落库记录。"""
    return await wait_until(
        lambda: _turn_if_match(store, session_id, seq, status), timeout
    )


def _turn_if_match(
    store: SessionStore, session_id: str, seq: int, status: str
) -> dict[str, Any] | None:
    turn = store.turn(session_id, seq)
    if turn is not None and turn["status"] == status:
        return turn
    return None


def make_orchestrator(
    settings: Settings,
    tmp_path: Path,
    *,
    transport: Any | None = None,
    vad: Any | None = None,
    providers: dict[str, Any] | None = None,
    tools: Any | None = None,
    memory: Any | None = None,
    registry: ProviderRegistry | None = None,
    metrics: MetricsCollector | None = None,
    ui_sink: Callable[[str, dict[str, Any], int | None], None] | None = None,
) -> tuple[DialogOrchestrator, SessionStore]:
    """装配编排器与会话库；默认使用内存音频通道与静音 VAD。"""
    store = SessionStore(tmp_path / "session.sqlite3")
    orchestrator = DialogOrchestrator(
        settings,
        registry if registry is not None else ProviderRegistry(),
        metrics if metrics is not None else MetricsCollector(),
        store,
        transport=transport if transport is not None else FakeTransport(),
        vad=vad if vad is not None else FakeVad(),
        providers=dict(providers or {}),
        tools=tools,
        memory=memory,
        ui_sink=ui_sink,
    )
    return orchestrator, store
