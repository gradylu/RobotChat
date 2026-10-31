"""编排层 Phase 5 接线单测（T-5.5 / T-5.9 / T-5.10 / T-5.12、C-82 / C-83 / C-84 / C-176）。

覆盖：tool_call 派发与结果回灌、高风险二次确认超时取消、确认期语音判定与
Barge-in 禁用、长任务后台执行并播报、记忆落库与重启恢复、上下文裁剪与滚动摘要、
RAG 命中追溯。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from robotchat.audio.barge_in import BargeInDecision
from robotchat.audio.frame import AudioChunk, AudioFrame
from robotchat.config.loader import load_settings
from robotchat.core.cancel import CancellationToken
from robotchat.core.orchestrator import DialogOrchestrator
from robotchat.core.state_machine import State
from robotchat.dialog.memory import SqliteMemoryStore
from robotchat.dialog.rag import RagService
from robotchat.dialog.session import SessionStore
from robotchat.health import HealthStatus
from robotchat.observability.metrics import BARGE_IN_ACCEPTED, MetricsCollector
from robotchat.providers.base import (
    ASREvent,
    ChatMessage,
    ChatRequest,
    KnowledgeHit,
    LLMCapabilities,
    LLMEvent,
    TTSCapabilities,
    TTSRequest,
)
from robotchat.providers.registry import ProviderRegistry
from robotchat.tools.base import ToolCall, ToolDefinition, ToolResult
from robotchat.tools.policy import ToolPolicy
from robotchat.tools.registry import ToolRegistry, build_tool_registry

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
RATE = 16000
FRAME = 320


class FakeTransport:
    name = "fake"

    def __init__(self, frames: int = 100) -> None:
        self.frames = frames
        self.played: list[AudioChunk] = []

    async def open(self) -> None:
        return None

    def capture(self):
        async def generator():
            for seq in range(self.frames):
                yield AudioFrame(
                    data=b"\x00\x00" * FRAME, sample_rate=RATE, seq=seq, ts_device=seq * 0.02
                )
                await asyncio.sleep(0.01)

        return generator()

    async def play(self, chunk: AudioChunk) -> None:
        self.played.append(chunk)

    async def flush(self) -> None:
        return None

    async def close(self) -> None:
        return None


class FakeVad:
    name = "fake_vad"

    def process(self, frame: AudioFrame) -> None:
        del frame
        return None

    def reset(self) -> None:
        return None


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


class ToolCallingLLM:
    """首轮返回 tool_call，次轮返回文本；用于验证结果回灌后的第二轮请求。"""

    name = "tool_llm"
    capabilities = LLMCapabilities(streaming=True, tool_call=True, context_window=4096)

    def __init__(
        self,
        call: dict[str, Any] | None = None,
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
        if len(self.requests) == 1 and self.call is not None:
            yield LLMEvent(type="tool_call", tool_call=self.call)
            yield LLMEvent(type="done")
            return
        for piece in self.final:
            await asyncio.sleep(0)
            yield LLMEvent(type="token", text=piece)
        yield LLMEvent(type="done")


class SlowAdapter:
    """长任务桩：执行时长明显超过 async_threshold_s。"""

    name = "slow"

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.calls: list[ToolCall] = []
        self.finished = False

    async def execute(self, call: ToolCall, cancel: CancellationToken) -> ToolResult:
        del cancel
        self.calls.append(call)
        await asyncio.sleep(self.delay)
        self.finished = True
        return ToolResult(ok=True, output="已到达充电桩")

    async def health(self) -> HealthStatus:
        return HealthStatus.ok("长任务桩")


class FakeRetriever:
    def __init__(self, hits: list[KnowledgeHit]) -> None:
        self.hits = hits

    async def retrieve(
        self, query: str, top_k: int, cancel: Any
    ) -> list[KnowledgeHit]:
        del query, top_k, cancel
        return list(self.hits)


@pytest.fixture()
def settings(tmp_path: Path):
    return load_settings(
        config_dir=REPO_CONFIG,
        data_dir=tmp_path,
        environ={
            "ROBOTCHAT__RESILIENCE__OFFLINE_PROBE_ON_START": "false",
            "ROBOTCHAT__STATE__HEARTBEAT_MS": "5000",
            "ROBOTCHAT__PROVIDERS__ASR__FALLBACK": "",
            "ROBOTCHAT__PROVIDERS__LLM__FALLBACK": "",
            "ROBOTCHAT__PROVIDERS__TTS__FALLBACK": "",
            # 二次确认等待上限缩短，避免单测等待 10s（FR-9.10）
            "ROBOTCHAT__TOOLS__POLICY__CONFIRM_TIMEOUT_MS": "50",
        },
    )


def make_orchestrator(
    settings,
    store: SessionStore,
    *,
    llm: Any,
    tts: Any | None = None,
    tools: ToolRegistry | None = None,
    memory: Any | None = None,
    rag: RagService | None = None,
    ui_sink: Callable[[str, dict[str, Any], int | None], None] | None = None,
) -> DialogOrchestrator:
    return DialogOrchestrator(
        settings,
        ProviderRegistry(),
        MetricsCollector(),
        store,
        transport=FakeTransport(),
        vad=FakeVad(),
        providers={"llm": llm, "tts": tts or FakeTTS()},
        tools=tools,
        memory=memory,
        rag=rag,
        ui_sink=ui_sink,
    )


async def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"条件未在 {timeout}s 内满足")


async def test_tool_call_is_dispatched_and_result_fed_back(
    settings, tmp_path: Path
) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    llm = ToolCallingLLM({"id": "call-1", "name": "speak", "arguments": {"text": "你好呀"}})
    store = SessionStore(tmp_path / "session.sqlite3")
    tools = build_tool_registry(settings.tools)
    speak = tools.get("speak")
    assert speak is not None
    # 配置里播报超时 10s 会被判为长任务；此处缩短到 1s 走同步路径验证结果回灌
    tools.register(
        ToolDefinition(
            name=speak.name,
            description=speak.description,
            risk=speak.risk,
            parameters=speak.parameters,
            timeout_s=1.0,
            adapter=speak.adapter,
        )
    )
    orchestrator = make_orchestrator(
        settings,
        store,
        llm=llm,
        tools=tools,
        ui_sink=lambda kind, payload, turn_id: emitted.append((kind, payload)),
    )

    await orchestrator.start()
    try:
        result = await orchestrator.handle_text("播报你好呀")

        assert result["state"] == "ACTIVE_IDLE"
        assert len(llm.requests) == 2
        names = {item.name for item in llm.requests[0].tools}
        assert names == {"navigate_to", "set_io", "speak"}
        assert "优先调用已提供的工具" in llm.requests[0].messages[0].content
        fed_back = llm.requests[1].messages[-1]
        assert fed_back.role == "user"
        assert fed_back.content.startswith("[工具结果] speak")
        assert "你好呀" in fed_back.content

        snapshot = orchestrator.snapshot()["tools"]
        assert snapshot["enabled"] is True
        assert snapshot["calls"] == 1
        kinds = [kind for kind, _ in emitted]
        assert "llm.tool_call" in kinds
        assert "tool.result" in kinds
        tool_result = next(payload for kind, payload in emitted if kind == "tool.result")
        assert tool_result["ok"] is True
        assert tool_result["name"] == "speak"
    finally:
        await orchestrator.shutdown()
        store.close()


async def test_high_risk_tool_cancelled_when_confirmation_times_out(
    settings, tmp_path: Path
) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    llm = ToolCallingLLM(
        {"id": "call-1", "name": "navigate_to", "arguments": {"location": "充电桩"}},
        final=["已取消导航。"],
    )
    store = SessionStore(tmp_path / "session.sqlite3")
    tools = build_tool_registry(settings.tools)
    orchestrator = make_orchestrator(
        settings,
        store,
        llm=llm,
        tools=tools,
        ui_sink=lambda kind, payload, turn_id: emitted.append((kind, payload)),
    )

    await orchestrator.start()
    try:
        result = await orchestrator.handle_text("导航到充电桩")

        assert result["state"] == "ACTIVE_IDLE"
        assert tools.snapshot()["calls"] == 0
        assert len(llm.requests) == 2
        assert "未确认" in llm.requests[1].messages[-1].content

        kinds = [kind for kind, _ in emitted]
        assert "tool.confirm" in kinds
        assert "tool.confirm.resolved" in kinds
        resolved = next(
            payload for kind, payload in emitted if kind == "tool.confirm.resolved"
        )
        assert resolved["confirmed"] is False
        assert resolved["reason"] == "timeout"
        cancelled = next(payload for kind, payload in emitted if kind == "tool.result")
        assert cancelled["error_kind"] == "cancelled"
    finally:
        await orchestrator.shutdown()
        store.close()


async def test_confirmation_uses_voice_only_and_blocks_barge_in(
    settings, tmp_path: Path
) -> None:
    store = SessionStore(tmp_path / "session.sqlite3")
    tools = build_tool_registry(settings.tools)
    orchestrator = make_orchestrator(
        settings,
        store,
        llm=ToolCallingLLM(),
        tools=tools,
    )

    loop = asyncio.get_running_loop()
    orchestrator._confirming = True
    orchestrator._confirm_future = loop.create_future()
    try:
        await orchestrator._on_asr_during_confirm(ASREvent(type="partial", text="确"))
        assert not orchestrator._confirm_future.done()

        await orchestrator._on_asr_during_confirm(ASREvent(type="final", text="确认执行"))
        assert orchestrator._confirm_future.result() is True

        orchestrator._confirm_future = loop.create_future()
        await orchestrator._on_asr_during_confirm(ASREvent(type="final", text="随便吧"))
        assert orchestrator._confirm_future.result() is False

        orchestrator._confirm_future = loop.create_future()
        await orchestrator._on_asr_during_confirm(ASREvent(type="final", text="取消"))
        assert orchestrator._confirm_future.result() is False
        # 确认期语音不新建轮次
        assert orchestrator.turns.current_turn_id is None

        # 确认播报期间禁用 Barge-in（FR-9.10）
        orchestrator.machine._state = State.SPEAKING
        orchestrator._maybe_start_barge_in(BargeInDecision(accepted=True))
        assert orchestrator._barge_task is None
        assert orchestrator.metrics.counter(BARGE_IN_ACCEPTED) == 0
    finally:
        orchestrator._confirming = False
        orchestrator._confirm_future = None
        store.close()


async def test_long_task_runs_in_background_and_announces(
    settings, tmp_path: Path
) -> None:
    adapter = SlowAdapter()
    tools = ToolRegistry(ToolPolicy(), adapters={adapter.name: adapter})
    tools.register(
        ToolDefinition(
            name="long_task",
            description="前往远端地点",
            risk="low",
            parameters={"type": "object", "properties": {}, "required": []},
            timeout_s=60.0,
            adapter=adapter.name,
        )
    )
    llm = ToolCallingLLM(
        {"id": "call-1", "name": "long_task", "arguments": {}},
        final=["正在前往，请稍候。"],
    )
    store = SessionStore(tmp_path / "session.sqlite3")
    orchestrator = make_orchestrator(settings, store, llm=llm, tools=tools)

    await orchestrator.start()
    try:
        result = await orchestrator.handle_text("去远端地点")

        assert result["state"] == "ACTIVE_IDLE"
        assert len(llm.requests) == 2
        assert "后台执行" in llm.requests[1].messages[-1].content
        await _wait_until(
            lambda: adapter.finished
            and any(event["type"] == "task.result" for event in store.events("default"))
        )
        assert tools.snapshot()["calls"] == 1
        # 长任务不阻塞对话：轮次已经完成，任务结果随后播报
        turn = store.turns("default")[0]
        assert turn["status"] == "completed"
    finally:
        await orchestrator.shutdown()
        store.close()


async def test_memory_persists_and_restores_history(settings, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.sqlite3")
    memory = SqliteMemoryStore(store)
    orchestrator = make_orchestrator(
        settings,
        store,
        llm=ToolCallingLLM(final=["好的。"]),
        memory=memory,
    )

    await orchestrator.start()
    try:
        await orchestrator.handle_text("我叫小明")
        await _wait_until(lambda: memory.snapshot()["written"] >= 2)
        await memory.flush()

        history = await memory.history("default")
        assert [message.role for message in history] == ["user", "assistant"]
        assert history[0].content == "我叫小明"
        assert history[1].content == "好的。"
        assert orchestrator.snapshot()["memory"]["backend"] == "sqlite"
    finally:
        await orchestrator.shutdown()

    restored = make_orchestrator(
        settings,
        store,
        llm=ToolCallingLLM(final=["好的。"]),
        memory=SqliteMemoryStore(store),
    )
    await restored.start()
    try:
        contents = [message.content for message in restored._history]
        assert contents[:2] == ["我叫小明", "好的。"]
    finally:
        await restored.shutdown()
        store.close()


async def test_context_budget_trims_history_and_updates_summary(
    settings, tmp_path: Path
) -> None:
    store = SessionStore(tmp_path / "session.sqlite3")
    memory = SqliteMemoryStore(store)
    llm = ToolCallingLLM(final=["好的。"])
    llm.capabilities = LLMCapabilities(streaming=True, tool_call=True, context_window=256)
    orchestrator = make_orchestrator(settings, store, llm=llm, memory=memory)

    await orchestrator.start()
    try:
        orchestrator._history = [
            ChatMessage(role="assistant", content="很长的历史内容" * 20) for _ in range(20)
        ]
        await orchestrator.handle_text("现在呢？")

        # 上下文预算只保留最近一条历史，其余被裁掉
        kept_history = llm.requests[0].messages[1:-1]
        assert len(kept_history) == 1
        assert kept_history[0].content == "很长的历史内容" * 20

        summary = await memory.summary("default")
        assert "早期对话摘要" in summary
    finally:
        await orchestrator.shutdown()
        store.close()


async def test_rag_hits_are_recorded_on_turn(settings, tmp_path: Path) -> None:
    hits = [KnowledgeHit(doc_id="doc-1", text="充电桩位于 B 区。", score=0.9)]
    llm = ToolCallingLLM(final=["充电桩在 B 区。"])
    store = SessionStore(tmp_path / "session.sqlite3")
    orchestrator = make_orchestrator(
        settings,
        store,
        llm=llm,
        rag=RagService(FakeRetriever(hits)),
    )

    await orchestrator.start()
    try:
        await orchestrator.handle_text("充电桩在哪？")

        system_prompt = llm.requests[0].messages[0].content
        assert "【知识片段" in system_prompt
        assert "不可信数据" in system_prompt
        assert "[doc-1]" in system_prompt

        turn = store.turns("default")[0]
        assert turn["rag_hits"][0]["doc_id"] == "doc-1"
        assert turn["rag_hits"][0]["score"] == 0.9
        assert turn["rag_hits"][0]["skipped"] is False
    finally:
        await orchestrator.shutdown()
        store.close()
