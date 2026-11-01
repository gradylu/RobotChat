"""FR-17 / R14 意图识别与路由单测：规则快速路径、语义分类兜底、command 路由与落库。

覆盖：
- 规则快速路径命中 / 不误判（单字例词仅精确匹配）与 P95 ≤ 20ms 预算；
- route() 的确认窗口约束（INV-11）；
- 语义分类低置信度 / 超时 / 异常 / 槽位缺失一律兜底 chat（FR-17）；
- schema 非法配置拒绝（缺 chat、active 越界、threshold ≥ gray_zone_top）；
- 编排层：command 首音频前转工具执行（INV-10）、未知工具拒绝、晚到降级 chat、
  落库 intent 字段读回、stop 语义打断复用 barge-in 取消路径。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from robotchat.audio.frame import AudioChunk, AudioFrame
from robotchat.config.loader import load_settings
from robotchat.config.schema import IntentDef, IntentSettings
from robotchat.core.orchestrator import DialogOrchestrator
from robotchat.core.state_machine import State
from robotchat.dialog.intent import (
    INTENT_CANCEL,
    INTENT_CHAT,
    INTENT_COMMAND,
    INTENT_CONFIRM,
    INTENT_STOP,
    IntentRouter,
    IntentRuleMatcher,
)
from robotchat.dialog.session import SessionStore
from robotchat.health import HealthStatus
from robotchat.observability.metrics import (
    BARGE_IN_ACCEPTED,
    INTENT_FALLBACK,
    INTENT_LATE_ROUTE,
    INTENT_LOW_CONFIDENCE,
    INTENT_RULE,
    INTENT_TOTAL,
    MetricsCollector,
)
from robotchat.providers.base import (
    ASREvent,
    ChatRequest,
    IntentCapabilities,
    IntentRequest,
    IntentResult,
    LLMCapabilities,
    LLMEvent,
    TTSCapabilities,
    TTSRequest,
)
from robotchat.providers.registry import ProviderRegistry, register_provider
from robotchat.tools.base import ToolDefinition
from robotchat.tools.policy import ToolPolicy
from robotchat.tools.registry import ToolRegistry

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
RATE = 16000
FRAME = 320


# ---------------------------------------------------------------- 桩件


class FakeTransport:
    name = "fake"

    def __init__(self) -> None:
        self.played: list[AudioChunk] = []
        self.flush_count = 0

    async def open(self) -> None:
        return None

    def capture(self):
        async def generator():
            for seq in range(40):
                yield AudioFrame(
                    data=b"\x00\x00" * FRAME, sample_rate=RATE, seq=seq, ts_device=seq * 0.02
                )
                await asyncio.sleep(0.01)

        return generator()

    async def play(self, chunk: AudioChunk) -> None:
        self.played.append(chunk)

    async def flush(self) -> None:
        self.flush_count += 1

    async def close(self) -> None:
        return None


class FakeVad:
    name = "fake_vad"

    def process(self, frame: AudioFrame) -> None:
        del frame
        return None

    def reset(self) -> None:
        return None


class FakeASRStream:
    """打开后只产出 partial，关闭后才产出 final（barge-in 热流复用）。"""

    name = "fake_asr"

    def __init__(self, text: str = "你好") -> None:
        self.text = text
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
            yield ASREvent(type="partial", text=self.text[:3], lang="zh")
            await self.closed.wait()
            yield ASREvent(type="final", text=self.text, lang="zh")

        return generator()

    async def close(self) -> None:
        self.closed.set()


class FakeLLM:
    name = "fake_llm"
    capabilities = LLMCapabilities(streaming=True, tool_call=False, context_window=4096)

    def __init__(self, pieces: list[str]) -> None:
        self.pieces = pieces
        self.requests: list[ChatRequest] = []

    async def chat_stream(
        self, req: ChatRequest, cancel: Any
    ) -> AsyncIterator[LLMEvent]:
        del cancel
        self.requests.append(req)
        for piece in self.pieces:
            await asyncio.sleep(0)
            yield LLMEvent(type="token", text=piece)
        yield LLMEvent(type="done")


class HangingLLM:
    """无 token、done 前永久挂起：让轮次停在 THINKING，便于注入 stop 快速路径。"""

    name = "hanging_llm"
    capabilities = LLMCapabilities(streaming=True, tool_call=False, context_window=4096)

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    def release(self) -> None:
        self.gate.set()

    async def chat_stream(
        self, req: ChatRequest, cancel: Any
    ) -> AsyncIterator[LLMEvent]:
        del cancel
        self.requests.append(req)
        self.started.set()
        await self.gate.wait()
        yield LLMEvent(type="done")


class FakeTTS:
    name = "fake_tts"
    capabilities = TTSCapabilities(sample_rate=RATE)

    def __init__(self) -> None:
        self.requests: list[TTSRequest] = []

    async def synthesize_stream(
        self, req: TTSRequest, cancel: Any
    ) -> AsyncIterator[AudioChunk]:
        del cancel
        self.requests.append(req)
        await asyncio.sleep(0)
        yield AudioChunk(data=b"\x00\x00" * FRAME, sample_rate=RATE)


class StubAdapter:
    """最小工具适配器：记录调用并返回成功。"""

    name = "stub"

    def __init__(self, output: str = "执行成功") -> None:
        self.output = output
        self.calls: list[Any] = []

    async def execute(self, call: Any, cancel: Any) -> Any:
        del cancel
        self.calls.append(call)
        from robotchat.tools.base import ToolResult

        return ToolResult(ok=True, output=self.output)

    async def health(self) -> HealthStatus:
        return HealthStatus.ok("stub 适配器")


@register_provider("intent", "stub_intent")
class StubIntentProvider:
    """config 驱动的意图分类桩件：固定结论 / 延迟 / 异常，供路由用例编排。"""

    name = "stub_intent"
    capabilities = IntentCapabilities(
        intents=frozenset({"chat", "knowledge", "command"}), supports_slots=True
    )

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.intent = str(cfg.get("intent", INTENT_CHAT))
        self.confidence = float(cfg.get("confidence", 0.9))
        self.slots: dict[str, Any] = dict(cfg.get("slots", {}))
        self.delay_s = float(cfg.get("delay_s", 0.0))
        self.fail = str(cfg.get("fail", ""))
        self.requests: list[IntentRequest] = []

    async def classify(self, req: IntentRequest, cancel: Any) -> IntentResult:
        del cancel
        self.requests.append(req)
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError(self.fail)
        return IntentResult(
            intent=self.intent,
            confidence=self.confidence,
            slots=dict(self.slots),
            source="model",
            latency_ms=1.0,
        )

    async def health(self) -> HealthStatus:
        return HealthStatus.ok("stub intent")


# ---------------------------------------------------------------- 公共工具


def _settings(tmp_path: Path, **overrides: str):
    environ = {
        "ROBOTCHAT__RESILIENCE__OFFLINE_PROBE_ON_START": "false",
        "ROBOTCHAT__STATE__HEARTBEAT_MS": "5000",
        "ROBOTCHAT__PROVIDERS__WAKEWORD__ENABLED": "false",
        "ROBOTCHAT__PROVIDERS__ASR__FALLBACK": "",
        "ROBOTCHAT__PROVIDERS__LLM__FALLBACK": "",
        "ROBOTCHAT__PROVIDERS__TTS__FALLBACK": "",
    }
    environ.update(overrides)
    return load_settings(config_dir=REPO_CONFIG, data_dir=tmp_path, environ=environ)


async def _wait_until(predicate: Any, timeout: float = 3.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.005)
    raise AssertionError("等待条件超时")


def _orchestrator(
    settings: Any,
    tmp_path: Path,
    *,
    registry: ProviderRegistry | None = None,
    metrics: MetricsCollector | None = None,
    tools: ToolRegistry | None = None,
    providers: dict[str, Any] | None = None,
    intent_router: IntentRouter | None = None,
    ui_sink: Any = None,
) -> tuple[DialogOrchestrator, SessionStore, MetricsCollector]:
    metrics = metrics or MetricsCollector()
    store = SessionStore(tmp_path / "session.sqlite3")
    orchestrator = DialogOrchestrator(
        settings,
        registry if registry is not None else ProviderRegistry(),
        metrics,
        store,
        transport=FakeTransport(),
        vad=FakeVad(),
        providers=dict(providers or {}),
        tools=tools,
        intent_router=intent_router,
        ui_sink=ui_sink,
    )
    return orchestrator, store, metrics


def _diagnose_tools(adapter: StubAdapter) -> ToolRegistry:
    tools = ToolRegistry(ToolPolicy(), adapters={"stub": adapter})
    tools.register(
        ToolDefinition(
            name="diagnose",
            description="执行系统自检",
            risk="low",
            parameters={"type": "object", "properties": {}, "required": []},
            timeout_s=1.0,
            adapter="stub",
        )
    )
    return tools


def _stub_router(
    settings: Any,
    slot_config: dict[str, Any],
    metrics: MetricsCollector | None = None,
) -> tuple[IntentRouter, ProviderRegistry, StubIntentProvider]:
    registry = ProviderRegistry()
    registry.register_slot("intent", "stub_intent", slot_config)
    slot = registry.slot("intent")
    assert slot is not None and slot.primary.driver is not None
    provider = slot.primary.driver
    assert isinstance(provider, StubIntentProvider)
    router = IntentRouter(settings, registry, metrics)
    return router, registry, provider


# ---------------------------------------------------------------- 规则快速路径


def test_rule_fast_path_hits_and_avoids_false_positives(tmp_path: Path) -> None:
    """stop / confirm / cancel 例词命中；单字例词仅精确匹配不误判。"""
    settings = _settings(tmp_path)
    matcher = IntentRuleMatcher(settings.intent)

    def routed(text: str) -> str | None:
        hit = matcher.match(text)
        return None if hit is None else hit.intent

    # 命中：完整例词与中英文大小写
    assert routed("停止") == INTENT_STOP
    assert routed("停止播放") == INTENT_STOP
    assert routed("闭嘴") == INTENT_STOP
    assert routed("Stop") == INTENT_STOP
    assert routed("确认") == INTENT_CONFIRM
    assert routed("确认执行") == INTENT_CONFIRM  # ≥2 字例词允许子串命中
    assert routed("OK") == INTENT_CONFIRM
    assert routed("取消") == INTENT_CANCEL
    assert routed("算了，不弄了") == INTENT_CANCEL
    assert routed("no") == INTENT_CANCEL

    # 不误判：单字「停」不参与子串匹配，普通语句不走快速路径
    assert routed("停一下") is None
    assert routed("不停地说") is None
    assert routed("暂停一下") is None
    assert routed("你好") is None
    assert routed("今天天气怎么样") is None


def test_rule_fast_path_latency_p95_under_20ms(tmp_path: Path) -> None:
    """R14 验收：规则快速路径 P95 ≤ 20ms（500 次匹配计时）。"""
    settings = _settings(tmp_path)
    metrics = MetricsCollector()
    router = IntentRouter(settings, ProviderRegistry(), metrics)

    latencies = []
    for _ in range(500):
        hit = router.fast_path("停止播放")
        assert hit is not None and hit.source == "rule"
        latencies.append(hit.latency_ms)

    p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1]
    assert p95 <= 20.0
    assert metrics.summary(INTENT_RULE).count == 500


def test_route_applies_confirm_window_rules(tmp_path: Path) -> None:
    """INV-11：窗口外 confirm / cancel 一律按 chat；stop / command 不受窗口约束。"""
    settings = _settings(tmp_path)
    router = IntentRouter(settings, ProviderRegistry(), None)
    confirm = IntentResult(intent=INTENT_CONFIRM, confidence=1.0)
    cancel = IntentResult(intent=INTENT_CANCEL, confidence=1.0)

    assert router.route(confirm, confirm_window_open=True) == INTENT_CONFIRM
    assert router.route(confirm, confirm_window_open=False) == INTENT_CHAT
    assert router.route(cancel, confirm_window_open=True) == INTENT_CANCEL
    assert router.route(cancel, confirm_window_open=False) == INTENT_CHAT
    assert (
        router.route(IntentResult(intent=INTENT_STOP, confidence=1.0), False)
        == INTENT_STOP
    )
    assert (
        router.route(IntentResult(intent=INTENT_COMMAND, confidence=0.9), False)
        == INTENT_COMMAND
    )


# ---------------------------------------------------------------- 语义分类兜底


async def test_classify_low_confidence_falls_back_to_chat(tmp_path: Path) -> None:
    """INV-11：低于 threshold 的结论兜底 chat，且累计 low_confidence 计数。"""
    settings = _settings(tmp_path)
    metrics = MetricsCollector()
    router, _, _ = _stub_router(
        settings, {"intent": "knowledge", "confidence": 0.3}, metrics
    )

    result = await router.classify("机器人的保修政策是什么", "zh")
    assert result.intent == INTENT_CHAT
    assert result.confidence == 0.3  # 保留原始置信度供灰区观测
    assert metrics.counter(INTENT_TOTAL) == 1
    assert metrics.counter(INTENT_LOW_CONFIDENCE) == 1
    assert metrics.counter(INTENT_FALLBACK) == 0

    # 边界：confidence == threshold 时不判低（严格小于才兜底）
    router_edge, _, _ = _stub_router(
        settings, {"intent": "knowledge", "confidence": 0.6}
    )
    edge = await router_edge.classify("你好", "zh")
    assert edge.intent == "knowledge"


async def test_classify_timeout_error_and_missing_slot_fallback(tmp_path: Path) -> None:
    """超时 / 驱动异常 / 槽位缺失一律兜底 chat 并累计 fallback 计数（FR-17）。"""
    # ① 超时：驱动延迟超过预算
    settings = _settings(tmp_path, **{"ROBOTCHAT__INTENT__CLASSIFY_TIMEOUT_MS": "80"})
    metrics = MetricsCollector()
    router, _, _ = _stub_router(
        settings,
        {"intent": "knowledge", "confidence": 0.95, "delay_s": 0.5},
        metrics,
    )
    result = await router.classify("你好", "zh")
    assert result.intent == INTENT_CHAT
    assert result.confidence == 0.0
    assert metrics.counter(INTENT_FALLBACK) == 1

    # ② 驱动异常
    metrics_error = MetricsCollector()
    router_error, _, _ = _stub_router(
        _settings(tmp_path), {"fail": "下游分类服务不可用"}, metrics_error
    )
    result_error = await router_error.classify("你好", "zh")
    assert result_error.intent == INTENT_CHAT
    assert metrics_error.counter(INTENT_FALLBACK) == 1

    # ③ 槽位缺失（未注册 intent 驱动）
    router_missing = IntentRouter(_settings(tmp_path), ProviderRegistry(), MetricsCollector())
    result_missing = await router_missing.classify("你好", "zh")
    assert result_missing.intent == INTENT_CHAT


# ---------------------------------------------------------------- schema 校验


def test_intent_schema_rejects_invalid_config() -> None:
    """缺 chat / active 越界 / threshold ≥ gray_zone_top 的配置一律被拒。"""
    with pytest.raises(ValidationError):
        IntentSettings(intents={"stop": IntentDef(examples=["停"], fast_path=True)})
    with pytest.raises(ValidationError):
        IntentSettings(active="missing_driver")
    with pytest.raises(ValidationError):
        IntentSettings(confidence_threshold=0.9, gray_zone_top=0.8)


# ---------------------------------------------------------------- 编排层接入


async def test_command_intent_routes_to_tool_before_first_audio(tmp_path: Path) -> None:
    """INV-10 / FR-17：command 在首块音频出声前回收并转工具执行，LLM 文本不入播报。"""
    settings = _settings(tmp_path)
    adapter = StubAdapter(output="自检完成")
    tools = _diagnose_tools(adapter)
    metrics = MetricsCollector()
    router, _, _ = _stub_router(
        settings,
        {
            "intent": "command",
            "confidence": 0.95,
            "slots": {"tool": "diagnose", "arguments": {"mode": "quick"}},
        },
        metrics,
    )
    llm = FakeLLM(["好的。", "马上开始。"])
    tts = FakeTTS()
    emitted: list[tuple[str, dict[str, Any]]] = []
    orchestrator, store, metrics = _orchestrator(
        settings,
        tmp_path,
        metrics=metrics,
        tools=tools,
        providers={"llm": llm, "tts": tts},
        intent_router=router,
        ui_sink=lambda kind, payload, turn_id: emitted.append((kind, payload)),
    )
    await orchestrator.start()
    try:
        result = await orchestrator.handle_text("帮我自检")

        assert adapter.calls[0].name == "diagnose"
        assert adapter.calls[0].args == {"mode": "quick"}
        assert result is not None
        assert result["assistant_text"] == "好的，已完成。"
        # LLM 文本未进入播报队列，只有命令反馈合成
        assert [req.text for req in tts.requests] == ["好的，已完成。"]

        turn = store.turn("default", 1)
        assert turn["status"] == "completed"
        assert turn["intent"] == "command"
        assert turn["intent_source"] == "model"
        assert turn["intent_slots"] == {"tool": "diagnose", "arguments": {"mode": "quick"}}
        assert metrics.counter(INTENT_TOTAL) == 1
        assert metrics.counter("intent_distribution:command") == 1
        assert any(kind == "intent.detected" for kind, _ in emitted)
        assert any(
            kind == "llm.tool_call" and payload["name"] == "diagnose"
            for kind, payload in emitted
        )
    finally:
        await orchestrator.shutdown()
        store.close()


async def test_command_intent_unknown_tool_denied(tmp_path: Path) -> None:
    """command 指向未注册工具：不执行、播报不可用提示。"""
    settings = _settings(tmp_path)
    adapter = StubAdapter()
    tools = _diagnose_tools(adapter)
    router, _, _ = _stub_router(
        settings,
        {"intent": "command", "confidence": 0.95, "slots": {"tool": "nonexistent"}},
    )
    llm = FakeLLM(["好的。"])
    tts = FakeTTS()
    orchestrator, store, _ = _orchestrator(
        settings,
        tmp_path,
        tools=tools,
        providers={"llm": llm, "tts": tts},
        intent_router=router,
    )
    await orchestrator.start()
    try:
        result = await orchestrator.handle_text("帮我自检")

        assert adapter.calls == []
        assert result is not None
        assert result["assistant_text"] == "该功能暂不可用：未知工具 nonexistent"
        assert store.turn("default", 1)["intent"] == "command"
    finally:
        await orchestrator.shutdown()
        store.close()


async def test_late_classify_falls_back_to_chat(tmp_path: Path) -> None:
    """分类超预算（晚到）：该轮按 chat 继续，intent 留空并累计 late_route 计数。"""
    settings = _settings(tmp_path, **{"ROBOTCHAT__INTENT__CLASSIFY_TIMEOUT_MS": "60"})
    adapter = StubAdapter()
    tools = _diagnose_tools(adapter)
    metrics = MetricsCollector()
    router, _, _ = _stub_router(
        settings,
        {
            "intent": "command",
            "confidence": 0.95,
            "slots": {"tool": "diagnose"},
            "delay_s": 0.3,
        },
        metrics,
    )
    llm = FakeLLM(["你好。", "我是机器人。"])
    tts = FakeTTS()
    orchestrator, store, metrics = _orchestrator(
        settings,
        tmp_path,
        metrics=metrics,
        tools=tools,
        providers={"llm": llm, "tts": tts},
        intent_router=router,
    )
    await orchestrator.start()
    try:
        result = await orchestrator.handle_text("帮我自检")

        # 结论晚到：正常走 LLM 对话，不转工具
        assert result is not None
        assert result["assistant_text"] == "你好。我是机器人。"
        assert adapter.calls == []
        turn = store.turn("default", 1)
        assert turn["status"] == "completed"
        assert not turn["intent"]
        assert not turn["intent_slots"]
        assert metrics.counter(INTENT_TOTAL) == 1
        assert metrics.counter(INTENT_LATE_ROUTE) == 1
        assert metrics.counter(INTENT_FALLBACK) == 0
    finally:
        await orchestrator.shutdown()
        store.close()


async def test_knowledge_intent_recorded_in_session(tmp_path: Path) -> None:
    """语义结论正常生效：intent 五字段随轮次落库并可读回。"""
    settings = _settings(tmp_path)
    metrics = MetricsCollector()
    router, _, _ = _stub_router(
        settings,
        {"intent": "knowledge", "confidence": 0.9, "slots": {"topic": "保修"}},
        metrics,
    )
    llm = FakeLLM(["保修期", "一年。"])
    tts = FakeTTS()
    orchestrator, store, metrics = _orchestrator(
        settings,
        tmp_path,
        metrics=metrics,
        providers={"llm": llm, "tts": tts},
        intent_router=router,
    )
    await orchestrator.start()
    try:
        result = await orchestrator.handle_text("机器人的保修政策是什么")

        assert result is not None
        assert result["assistant_text"] == "保修期一年。"

        turn = store.turn("default", 1)
        assert turn["status"] == "completed"
        assert turn["intent"] == "knowledge"
        assert turn["intent_source"] == "model"
        assert 0.89 <= turn["intent_confidence"] <= 0.91
        assert turn["intent_slots"] == {"topic": "保修"}
        assert turn["intent_latency_ms"] >= 0
        assert metrics.counter(INTENT_TOTAL) == 1
        assert metrics.counter("intent_distribution:knowledge") == 1
    finally:
        await orchestrator.shutdown()
        store.close()


async def test_stop_intent_interrupts_turn_like_barge_in(tmp_path: Path) -> None:
    """stop 快速路径复用 barge-in 取消路径：轮次 interrupted、回 LISTENING、无幽灵播报。"""
    settings = _settings(tmp_path)
    metrics = MetricsCollector()
    router, _, _ = _stub_router(
        settings, {"intent": INTENT_CHAT, "confidence": 0.9}, metrics
    )
    asr = FakeASRStream("背景噪声")
    llm = HangingLLM()
    tts = FakeTTS()
    emitted: list[tuple[str, dict[str, Any]]] = []
    orchestrator, store, metrics = _orchestrator(
        settings,
        tmp_path,
        metrics=metrics,
        providers={"asr": asr, "llm": llm, "tts": tts},
        intent_router=router,
        ui_sink=lambda kind, payload, turn_id: emitted.append((kind, payload)),
    )
    await orchestrator.start()
    try:
        await orchestrator._on_speech_start()
        turn_id = orchestrator.turns.current_turn_id
        await orchestrator._on_asr_event(
            ASREvent(type="final", text="今天天气怎么样", lang="zh"), turn_id
        )
        await _wait_until(
            lambda: llm.started.is_set() and orchestrator.machine.state == State.THINKING
        )

        # 首块音频出声前说出「停止」：规则快速路径 → 语义打断
        await orchestrator._on_asr_event(
            ASREvent(type="final", text="停止", lang="zh"), turn_id
        )

        assert orchestrator.machine.state == State.LISTENING
        assert orchestrator.turns.current_turn_id == turn_id + 1
        record = store.turn("default", 1)
        assert record["status"] == "interrupted"
        assert record["barge_in"] == 1
        assert record["intent"] == INTENT_STOP
        assert record["intent_source"] == "rule"
        assert metrics.counter(BARGE_IN_ACCEPTED) == 1
        assert metrics.counter("intent_distribution:stop") == 1
        assert any(
            kind == "intent.detected" and payload["routed"] == INTENT_STOP
            for kind, payload in emitted
        )
        assert tts.requests == []  # 打断发生在任何音频出声前
    finally:
        llm.release()
        await orchestrator.shutdown()
        store.close()
