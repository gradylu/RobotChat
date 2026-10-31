"""全双工会话编排（T-3.6 / T-3.7）与能力协商（T-2.9）。

链路：音频采集 → VAD → ASR → 记忆 / RAG → LLM → 按句切分 → TTS 流式合成 → 播放队列。
关键点：
- 采集与播放是独立并发任务，播报期间采集不停止（INV-3）；
- LLM 首 token 即进入 `SPEAKING`，句子边生成边合成，不等待整段（C-44 / C-45）；
- 取消只作用于当前轮次对话链路（LLM / TTS / 播放），已移交执行层的后台任务保持运行。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
import wave
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from robotchat.audio.barge_in import BargeInDecision, BargeInDetector
from robotchat.audio.buffer import float_to_pcm16, rms_dbfs
from robotchat.audio.frame import AudioChunk, AudioFrame
from robotchat.audio.kws import KwsScheduler
from robotchat.audio.pipeline import AudioPipeline
from robotchat.audio.transport import AudioTransport
from robotchat.config.schema import BargeInSettings, Settings
from robotchat.core.cancel import CancellationToken
from robotchat.core.events import BoundedEventBus, Event, EventType
from robotchat.core.state_machine import MachineContext, State, StateMachine
from robotchat.core.turn import TurnManager
from robotchat.core.watchdog import Watchdog
from robotchat.dialog.language import LanguageRouter, VoicePlan
from robotchat.dialog.prompt import ContextResult, assemble_context, build_system_prompt
from robotchat.dialog.rag import RagOutcome, RagService
from robotchat.dialog.session import SessionStore
from robotchat.observability.logging import get_logger
from robotchat.observability.metrics import (
    ACTIVE_WINDOW_EXPIRED,
    ASR_FINAL,
    BARGE_IN,
    BARGE_IN_ACCEPTED,
    BARGE_IN_ECHO_SUPPRESSED,
    BARGE_IN_REJECTED,
    E2E_FIRST_RESPONSE,
    LLM_FIRST_TOKEN,
    OPEN_EAR_CLOUD_AUDIO,
    STANDBY_RESULT_CACHED,
    TOOL_EXECUTION,
    TTS_FIRST_CHUNK,
    WAKE_FALSE_POSITIVE,
    WAKE_LATENCY,
    MetricsCollector,
)
from robotchat.providers.base import (
    ASREvent,
    ASRStream,
    ChatMessage,
    ChatRequest,
    TTSRequest,
    VadEvent,
    WakeEvent,
)
from robotchat.providers.registry import ProviderRegistry
from robotchat.tools.base import ToolCall, ToolDefinition, ToolResult
from robotchat.tools.policy import CONFIRM_ANSWER
from robotchat.tools.registry import ToolRegistry

logger = get_logger("robotchat.core.orchestrator")

SYSTEM_PROMPT = (
    "你是一个机器人语音助手。回答简洁、口语化，直接给出结论；"
    "不要使用 Markdown、表情符号或音标。"
)
SENTENCE_TERMINATORS = "。！？；\n.!?;"
SOFT_BREAKS = "，,、：:"


# ---------------------------------------------------------------- 能力协商（T-2.9）


@dataclass(frozen=True, slots=True)
class EffectiveCapabilities:
    streaming: bool = True
    tools_enabled: bool = False
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "streaming": self.streaming,
            "tools_enabled": self.tools_enabled,
            "notes": list(self.notes),
        }


def negotiate_llm_capabilities(
    provider: Any | None, tools_requested: bool = True
) -> EffectiveCapabilities:
    """读取 LLM capabilities 并决定是否启用工具调用。"""
    if provider is None:
        return EffectiveCapabilities(
            streaming=False, tools_enabled=False, notes=("LLM 驱动不可用",)
        )
    caps = getattr(provider, "capabilities", None)
    streaming = bool(getattr(caps, "streaming", True))
    tool_call = bool(getattr(caps, "tool_call", False))
    notes: list[str] = []
    tools_enabled = tools_requested and tool_call
    if tools_requested and not tool_call:
        notes.append("驱动不支持 tool call，任务指令能力已禁用")
        logger.warning(
            "tools_disabled_by_capabilities",
            provider=getattr(provider, "name", "unknown"),
        )
    if not streaming:
        notes.append("驱动不支持流式输出，首响指标可能劣化")
    return EffectiveCapabilities(
        streaming=streaming, tools_enabled=tools_enabled, notes=tuple(notes)
    )


def build_provider_report(
    registry: ProviderRegistry, tools_requested: bool = True
) -> dict[str, Any]:
    """`/api/providers` 内容：能力声明 + 熔断状态 + 实际生效的功能。"""
    report: dict[str, Any] = {}
    for kind, slot in registry.slots().items():
        item = slot.snapshot()
        item["capabilities"] = slot.capabilities()
        if kind == "llm":
            driver = slot.primary.driver if slot.primary.usable else None
            if driver is None and slot.fallback is not None:
                driver = slot.fallback.driver
            effective = negotiate_llm_capabilities(driver, tools_requested)
            item["features"] = effective.as_dict()
        report[kind] = item
    return report


# ---------------------------------------------------------------- 按句切分


class SentenceSplitter:
    """流式 token → 句子：遇到句末标点立即切分，过长则按软分隔 / 硬上限切分。"""

    def __init__(self, max_chars: int = 60) -> None:
        if max_chars < 1:
            raise ValueError("max_chars 必须 >= 1")
        self.max_chars = max_chars
        self._buffer = ""

    def push(self, text: str) -> list[str]:
        self._buffer += text
        return self._split(force=False)

    def flush(self) -> list[str]:
        return self._split(force=True)

    def _split(self, force: bool) -> list[str]:
        pieces: list[str] = []
        while True:
            terminator = self._first_terminator()
            if terminator is not None:
                piece = self._buffer[: terminator + 1].strip()
                self._buffer = self._buffer[terminator + 1 :]
                if piece:
                    pieces.append(piece)
                continue
            if force:
                piece = self._buffer.strip()
                self._buffer = ""
                if piece:
                    pieces.append(piece)
                return pieces
            if len(self._buffer) >= self.max_chars:
                cut = self._soft_cut()
                piece = self._buffer[:cut].strip()
                self._buffer = self._buffer[cut:]
                if piece:
                    pieces.append(piece)
                continue
            return pieces

    def _first_terminator(self) -> int | None:
        indexes = [self._buffer.find(mark) for mark in SENTENCE_TERMINATORS]
        found = [index for index in indexes if index >= 0]
        return min(found) if found else None

    def _soft_cut(self) -> int:
        window = self._buffer[: self.max_chars]
        indexes = [window.rfind(mark) for mark in SOFT_BREAKS]
        found = [index + 1 for index in indexes if index >= 0]
        return max(found) if found else self.max_chars


# ---------------------------------------------------------------- 轮次


@dataclass
class DialogTurn:
    turn_id: int
    db_id: int
    lang: str
    user_text: str = ""
    assistant_text: str = ""
    asr_driver: str = ""
    tts_driver: str = ""
    tts_voice: str = "default"
    tts_lang: str = ""
    tts_fallback: bool = False
    tts_noted: bool = False
    asr_partials: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    rag_hit_ids: list[str] = field(default_factory=list)
    # 本轮知识命中追溯：doc_id / score / skipped（T-5.12 / C-176）
    rag_hits: list[dict[str, Any]] = field(default_factory=list)
    # 本轮工具调用记录（T-5.5 / 审计）
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    status: str = "active"
    started_at: float = field(default_factory=time.monotonic)
    speech_end_at: float | None = None
    # 已送入播放队列的助手文本片段（FR-6.8：打断时仅这部分入记忆，T-4.5）
    spoken_fragments: list[str] = field(default_factory=list)
    spoken_ratio: float = 0.0
    barge_in_stages: dict[str, float] = field(default_factory=dict)
    finished: bool = False


UIEventSink = Callable[[str, dict[str, Any], int | None], Awaitable[None] | None]


class DialogOrchestrator:
    """状态机 + 看门狗 + 音频管线 + provider 的全链路编排。"""

    def __init__(
        self,
        settings: Settings,
        registry: ProviderRegistry,
        metrics: MetricsCollector,
        store: SessionStore,
        *,
        rag: RagService | None = None,
        memory: Any | None = None,
        tools: ToolRegistry | None = None,
        session_id: str = "default",
        transport: AudioTransport | None = None,
        vad: Any | None = None,
        providers: dict[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        ui_sink: UIEventSink | None = None,
        event_bus: BoundedEventBus | None = None,
        aec_mode: str = "software",
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.metrics = metrics
        self._store = store
        self.rag = rag
        self.memory = memory
        self.tools = tools
        self.session_id = session_id
        self._clock = clock
        self._transport = transport
        self._vad = vad
        self._providers: dict[str, Any] = dict(providers or {})

        self.context = MachineContext(
            wakeword_enabled=settings.providers.wakeword.enabled
        )
        self.machine = StateMachine(
            timeouts_ms=settings.state.timeouts_ms,
            resident_states=settings.state.resident_states,
            context=self.context,
        )
        self.turns = TurnManager(session_id)
        self.bus = event_bus or BoundedEventBus(1024)
        self.watchdog = Watchdog(
            self.machine,
            heartbeat_ms=settings.state.heartbeat_ms,
            on_timeout=self._on_state_timeout,
            on_stall=self._on_stall,
            clock=clock,
        )

        self._root_token = CancellationToken("session")
        self._turn_token: CancellationToken | None = None
        self._turn_task: asyncio.Task[Any] | None = None
        self._current_turn: DialogTurn | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()

        self._pipeline: AudioPipeline | None = None
        self._asr_stream: ASRStream | None = None
        self._asr_task: asyncio.Task[Any] | None = None

        # 唤醒词与待机（T-3A.*）
        self._kws: KwsScheduler | None = None
        self._kws_error = ""
        self._active_window_task: asyncio.Task[Any] | None = None
        self._active_window_deadline: float | None = None
        self._pending_standby: deque[str] = deque()
        self._beep_cache: list[AudioChunk] | None = None
        self._asr_cloud = self._detect_cloud_asr()
        # 麦克风静音（C-93）：静音期间采集帧被抑制，恢复后立即可用
        self._muted = False

        # Barge-in 检测器（T-4.1）：保护窗口随 AEC 实际模式自适应（T-4.9 / C-169）
        self.aec_mode = aec_mode
        self.barge = BargeInDetector(settings.barge_in, aec_mode, clock=clock)
        self._barge_task: asyncio.Task[Any] | None = None
        self._barge_asr_task: asyncio.Task[Any] | None = None
        self._last_barge: dict[str, Any] | None = None

        self._ui_sinks: list[UIEventSink] = [ui_sink] if ui_sink is not None else []
        self._history: list[ChatMessage] = []
        self._history_limit = max(1, settings.memory.max_turns) * 2
        self._summary = ""
        # 高风险工具二次确认（T-5.10 / FR-9.10）：确认期间不启用 Barge-in
        self._confirming = False
        self._confirm_future: asyncio.Future[bool] | None = None
        self.language = LanguageRouter.from_settings(
            settings,
            driver_languages=lambda name: registry.driver_languages("asr", name),
            prepare=self._prewarm_driver,
        )

        self._running = False
        self._stopping = False
        self.turn_count = 0
        self.error_count = 0

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._running:
            return
        self._store.ensure_session(
            self.session_id,
            {
                "env": self.settings.app.env,
                "transport": self.settings.audio.transport,
                "wakeword": self.settings.providers.wakeword.enabled,
            },
        )
        if self._transport is None:
            self._transport = self.registry.create_active(
                "transport",
                self.settings.audio.transport,
                dict(self.settings.audio.model_dump()),
            )
        if self._vad is None:
            backend = "silero_vad" if self.settings.vad.backend == "silero" else "energy_vad"
            self._vad = self.registry.create_active(
                "vad", backend, dict(self.settings.vad.model_dump())
            )
        self._pipeline = AudioPipeline(
            self._transport,
            vad=self._vad,
            capture=self.settings.audio.capture,
            playback=self.settings.audio.playback,
            on_frame=self._on_frame,
            on_vad=self._on_vad,
            sample_rate=self.settings.audio.capture.target_sample_rate,
        )
        await self._pipeline.start()
        await self.bus.start()
        await self.watchdog.start()
        if self.memory is not None:
            await self.memory.start()
            await self._restore_history()
        self._running = True
        await self._handle(Event(EventType.EV_READY, session_id=self.session_id), None)
        if self.context.wakeword_enabled:
            await self._sync_wake_state(self.machine.state)
        elif self.machine.state == State.ACTIVE_IDLE:
            # 常开聆听：不存在待机态，ASR 热流常开（FR-14.8 / FR-14.12）
            await self._open_asr(preopen=True)
        logger.info(
            "dialog_started",
            session_id=self.session_id,
            state=self.machine.state.value,
            wakeword=self.context.wakeword_enabled,
        )

    async def shutdown(self, graceful: bool = True, timeout_s: float | None = None) -> None:
        """优雅关停：等待当前轮次完成后释放音频与模型连接。"""
        self._stopping = True
        if graceful:
            await self._wait_current_turn(timeout_s)
        await self._cancel_turn("shutdown")
        await self._cancel_barge_tasks()
        self.barge.mark_playback_stop()
        self._root_token.cancel("shutdown")
        self._cancel_active_window()
        if self._kws is not None:
            await self._kws.stop()
        await self._close_asr()
        if self._pipeline is not None:
            await self._pipeline.stop()
            self._pipeline = None
        await self.watchdog.stop()
        await self.bus.stop()
        if self.memory is not None:
            with contextlib.suppress(Exception):
                await self.memory.close()
        if self._running:
            await self._emit_ui("session.ended", {"state": self.machine.state.value}, None)
            self._store.end_session(self.session_id)
        self._running = False
        logger.info("dialog_stopped", session_id=self.session_id, turns=self.turn_count)

    async def _restore_history(self) -> None:
        """容器重启后从记忆库恢复多轮上下文（C-70 / C-126）。"""
        if self.memory is None:
            return
        try:
            history = await self.memory.history(self.session_id, self._history_limit)
            self._summary = await self.memory.summary(self.session_id)
        except Exception:
            logger.exception("memory_restore_failed")
            return
        if history and not self._history:
            self._history = list(history)
        if self._summary:
            logger.info("memory_summary_restored", chars=len(self._summary))

    async def _wait_current_turn(self, timeout_s: float | None) -> None:
        task = self._turn_task
        if task is None or task.done():
            return
        timeout = timeout_s
        if timeout is None:
            timeout_ms = self.machine.timeout_ms(self.machine.state) or 5000
            timeout = timeout_ms / 1000.0
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("graceful_shutdown_timeout", state=self.machine.state.value)
        except Exception:
            logger.exception("graceful_shutdown_turn_failed")

    # ------------------------------------------------------------ 对外入口

    def subscribe_ui(self, sink: UIEventSink) -> None:
        if sink not in self._ui_sinks:
            self._ui_sinks.append(sink)

    def unsubscribe_ui(self, sink: UIEventSink) -> None:
        if sink in self._ui_sinks:
            self._ui_sinks.remove(sink)

    @property
    def transport(self) -> AudioTransport | None:
        """当前音频传输实例（诊断回环复用，避免重复打开设备）。"""
        return self._transport

    def capture_stats(self) -> dict[str, Any]:
        """音频管线计数快照（自检第 4 项：采集活性，T-7.5）。"""
        pipeline = self._pipeline
        if pipeline is None:
            return {"running": False, "captured_frames": 0, "played_chunks": 0}
        return {
            "running": True,
            "captured_frames": pipeline.captured_frames,
            "played_chunks": pipeline.played_chunks,
            "dropped_chunks": pipeline.dropped_chunks,
            "capture_errors": pipeline.capture_errors,
        }

    async def handle_text(self, text: str, lang: str | None = None) -> dict[str, Any] | None:
        """文本输入兜底（C-92）：待机 / 活跃态均视作显式唤醒，跳过 ASR。"""
        text = text.strip()
        if not text:
            return None
        if lang:
            # 文本入口显式指定语言：直接切到该语言并立即生效（T-2A.7）
            self.language.set_mode(lang=lang)
        if self._current_turn is not None or self.machine.state not in (
            State.IDLE,
            State.ACTIVE_IDLE,
        ):
            await self._emit_ui(
                "error",
                {"code": "busy", "message": "当前轮次未结束，稍后再试", "recoverable": True},
                self.turns.current_turn_id,
            )
            return None
        turn = self._begin_turn(text)
        await self._handle(Event(EventType.EV_TEXT_INPUT, turn.turn_id, self.session_id), turn)
        await self._run_turn(turn)
        return {
            "turn_id": turn.turn_id,
            "state": self.machine.state.value,
            "assistant_text": turn.assistant_text,
        }

    async def reset(self, session_id: str | None = None) -> dict[str, Any]:
        """手动强制复位（T-3.9 / EV_RESET）：取消当前链路并重建到可用状态。"""
        subject = session_id or self.session_id
        await self._cancel_turn("reset")
        self._confirming = False
        self._confirm_future = None
        await self._cancel_barge_tasks()
        self.barge.reset()
        self._cancel_active_window()
        if self._kws is not None:
            await self._kws.stop()
        await self._close_asr()
        if self._pipeline is not None:
            with contextlib.suppress(Exception):
                await self._pipeline.flush()
        self._history.clear()
        self._summary = ""
        if self.memory is not None:
            with contextlib.suppress(Exception):
                await self.memory.clear(self.session_id)
        self.language.reset()
        self.turns.end_turn("aborted")
        await self._handle(Event(EventType.EV_RESET, session_id=subject), None)
        await self._handle(Event(EventType.EV_READY, session_id=subject), None)
        await self._emit_ui(
            "session.reset", {"state": self.machine.state.value}, None
        )
        logger.info("session_reset", session_id=subject, state=self.machine.state.value)
        return {
            "session_id": subject,
            "state": self.machine.state.value,
            "reset": True,
        }

    async def manual_wake(self, phrase: str = "manual") -> dict[str, Any]:
        """手动唤醒（T-6.7 / FR-11.2）：待机态直接激活并开启活跃窗口，不依赖唤醒词。"""
        if self.machine.state == State.ACTIVE_IDLE:
            self.refresh_active_window()
            return {"woken": False, "state": self.machine.state.value, "reason": "already_active"}
        if self.machine.state != State.IDLE:
            return {"woken": False, "state": self.machine.state.value, "reason": "busy"}
        await self._emit_ui(
            "wake.detected", {"phrase": phrase, "confidence": 1.0, "source": "manual"}, None
        )
        await self._handle(Event(EventType.EV_WAKE_DETECTED, session_id=self.session_id), None)
        if self.settings.providers.wakeword.preopen_asr:
            await self._open_asr(preopen=True)
        await self._handle(Event(EventType.EV_WAKE_ACK_DONE, session_id=self.session_id), None)
        logger.info("manual_wake", session_id=self.session_id)
        return {"woken": True, "state": self.machine.state.value}

    async def sleep(self) -> dict[str, Any]:
        """立即休眠（T-6.7）：关闭活跃窗口、释放 ASR，回到待机 / 常开聆听。"""
        self._cancel_active_window()
        await self._close_asr()
        if self.machine.state != State.IDLE:
            await self._handle(Event(EventType.EV_SESSION_END, session_id=self.session_id), None)
        return {"state": self.machine.state.value}

    async def interrupt(self, reason: str = "manual") -> dict[str, Any]:
        """手动打断（T-6.7 / C-93）：与语音打断走同一条执行序列。"""
        if self.machine.state != State.SPEAKING:
            return {"accepted": False, "reason": "not_speaking", "state": self.machine.state.value}
        decision = BargeInDecision(accepted=True, trigger="manual", reason=reason)
        await self._on_barge_in(decision)
        return {"accepted": True, "state": self.machine.state.value, "last": self._last_barge}

    async def set_muted(self, muted: bool) -> dict[str, Any]:
        """麦克风静音 / 恢复（C-93）：静音期间音频帧不进入 VAD / ASR / 打断判定。"""
        self._muted = bool(muted)
        await self._emit_ui(
            "mic.state",
            {"muted": self._muted, "state": self.machine.state.value},
            None,
        )
        logger.info("mic_muted" if self._muted else "mic_unmuted", session_id=self.session_id)
        return {"muted": self._muted}

    def dispatch_background(
        self, coro: Coroutine[Any, Any, Any], name: str = "background"
    ) -> asyncio.Task[Any]:
        """移交执行层的异步任务（工具 / 长任务）：看门狗恢复不会取消它们。"""
        task: asyncio.Task[Any] = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    # ------------------------------------------------------------ 音频回调

    async def _on_frame(self, frame: AudioFrame, cleaned: Any) -> None:
        self.watchdog.mark_progress("audio_frame")
        if self._muted:
            return
        self._observe_barge_energy(cleaned)
        kws = self._kws
        if kws is not None and kws.running:
            await kws.feed(frame)
        if (
            not self.context.wakeword_enabled
            and self._asr_stream is not None
            and self._asr_cloud
        ):
            # 常开聆听 + 云端 ASR：音频持续上传，按帧累计上传时长（FR-14.8）
            samples = len(frame.data) / (2 * max(1, frame.channels))
            self.metrics.increment(
                OPEN_EAR_CLOUD_AUDIO,
                max(1, int(samples * 1000.0 / max(1, frame.sample_rate))),
            )
        stream = self._asr_stream
        if stream is not None:
            try:
                await stream.feed(frame)
            except Exception:
                logger.exception("asr_feed_failed")
                await self._close_asr()

    def _observe_barge_energy(self, cleaned: Any) -> None:
        """播报期逐帧能量判定；接受即异步启动打断序列，不阻塞采集链路（T-4.1）。"""
        if cleaned is None:
            # 无 AEC 输出（直通 / 测试注入）时不做能量判定
            return
        decision = self.barge.observe_frame(rms_dbfs(cleaned))
        self._maybe_start_barge_in(decision)

    async def _on_vad(self, event: VadEvent) -> None:
        self.watchdog.mark_progress("vad")
        if self._muted:
            return
        await self._emit_ui(
            "vad.level",
            {"type": event.type, "energy_db": round(event.energy_db, 2)},
            self.turns.current_turn_id,
        )
        if event.type == "speech_start":
            await self._on_speech_start()
        elif event.type == "speech_end":
            await self._on_speech_end()

    async def _on_speech_start(self) -> None:
        state = self.machine.state
        if state in (State.ACTIVE_IDLE, State.WAKING):
            if self._current_turn is not None:
                return
            turn = self._begin_turn("")
            await self._handle(
                Event(EventType.EV_SPEECH_START, turn.turn_id, self.session_id), turn
            )
            await self._open_asr()
        elif state == State.LISTENING:
            await self._handle(
                Event(EventType.EV_SPEECH_START, self.turns.current_turn_id, self.session_id),
                self._current_turn,
            )
        elif state == State.SPEAKING:
            # 播报中用户起音：异步拉起 ASR 热流（partial 用于语义确认，不阻塞采集）
            self._schedule_barge_asr()

    async def _on_speech_end(self) -> None:
        turn = self._current_turn
        if turn is not None:
            turn.speech_end_at = self._clock()
        stream = self._asr_stream
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.close()

    # ------------------------------------------------------------ ASR

    async def _open_asr(self, preopen: bool = False) -> None:
        if self._asr_stream is not None:
            return
        hint = self.language.asr_lang_hint()
        decision = self.language.decision(hint, changed=False)
        driver = decision.asr_driver
        lang = decision.lang
        provider = await self._acquire_asr(driver)
        if provider is None and lang != self.language.fallback and not preopen:
            # 方言 ASR 不可达 / 熔断：本轮退回 fallback 语言并显式告警（FR-15.15）
            await self._degrade_language(lang, "方言 ASR 不可达")
            decision = self.language.decision(self.language.fallback, changed=False)
            driver = decision.asr_driver
            lang = decision.lang
            provider = await self._acquire_asr(driver)
        if provider is None:
            if preopen:
                # 播报期热流只服务语义确认，取不到时静默降级为纯能量判定（T-4.2），
                # 不得改动进行中轮次的语言与音色
                logger.info("barge_asr_unavailable", driver=driver, lang=lang)
                return
            await self._emit_ui(
                "error",
                {"code": "asr_unavailable", "message": "ASR 驱动不可用", "recoverable": True},
                self.turns.current_turn_id,
            )
            return
        turn = self._current_turn
        if turn is not None and not preopen:
            turn.lang = lang
            turn.asr_driver = driver
            self._apply_voice(turn, decision.voice)
        try:
            stream = await provider.open_stream(self.session_id, self._root_token, lang=lang)
        except Exception as exc:
            self._record_asr_failure(driver, repr(exc))
            logger.exception("asr_open_failed", driver=driver)
            await self._emit_ui(
                "error",
                {"code": "asr_open_failed", "message": repr(exc), "recoverable": True},
                self.turns.current_turn_id,
            )
            return
        self.registry.record_named_success("asr", driver)
        self._asr_stream = stream
        turn_id = None if preopen else self.turns.current_turn_id
        self._asr_task = asyncio.create_task(
            self._consume_asr(stream, turn_id), name=f"asr-consumer-{turn_id}"
        )

    async def _acquire_asr(self, driver: str) -> Any | None:
        """按语言路由取 ASR 驱动：主驱动走槽位，方言驱动按名取用（T-2A.2）。"""
        if "asr" in self._providers:
            return self._providers["asr"]
        if not driver or driver == self.settings.providers.asr.active:
            return await self._acquire("asr")
        config = dict(self.settings.providers.asr.drivers.get(driver, {}))
        try:
            entry = await self.registry.acquire_named("asr", driver, config)
        except Exception as exc:
            logger.warning("named_asr_unavailable", driver=driver, detail=repr(exc))
            return None
        self.watchdog.mark_progress("provider_asr")
        return entry.driver

    def _record_asr_failure(self, driver: str, reason: str) -> None:
        if driver == self.settings.providers.asr.active:
            self.registry.record_failure("asr", reason)
        else:
            self.registry.record_named_failure("asr", driver, reason)

    async def _degrade_language(self, lang: str, reason: str) -> None:
        """方言链路故障时本轮退回 fallback 语言，并计数 + 告警（FR-15.15）。"""
        self.language.note_language_fallback(reason, lang=lang)
        target = self.language.fallback
        turn = self._current_turn
        if turn is not None:
            turn.lang = target
            self._apply_voice(turn, self.language.voice_plan(target))
        await self._emit_ui(
            "language.fallback",
            {"from": lang, "to": target, "reason": reason},
            self.turns.current_turn_id,
        )

    async def _consume_asr(self, stream: ASRStream, turn_id: int | None) -> None:
        try:
            async for event in stream.events():
                effective = turn_id if turn_id is not None else self.turns.current_turn_id
                if effective is not None and not self.turns.accept(effective):
                    continue
                await self._on_asr_event(event, effective)
                if event.type == "final":
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("asr_consumer_failed")
            self.error_count += 1
        finally:
            if self._asr_stream is stream:
                self._asr_stream = None

    async def _on_asr_event(self, event: ASREvent, turn_id: int | None) -> None:
        turn = self._current_turn
        if self._confirming:
            # 二次确认期：语音只做确认 / 取消判定，不新建轮次（FR-9.10 / T-5.10）
            await self._on_asr_during_confirm(event)
            return
        if self.machine.state in (State.SPEAKING, State.INTERRUPTING):
            # 播报期的 ASR 结果只用于打断语义确认，不得污染当前轮次（T-4.2 / C-07）
            await self._on_asr_during_playback(event, turn_id)
            return
        if event.type == "partial":
            if turn is None or self.machine.state not in (State.LISTENING, State.WAKING):
                return
            turn.asr_partials.append(event.text)
            await self._emit_ui(
                "asr.partial", {"text": event.text, "lang": event.lang}, turn_id
            )
            await self._handle(
                Event(EventType.EV_ASR_PARTIAL, turn_id, self.session_id), turn
            )
            return
        if event.type == "error":
            self.error_count += 1
            self.language.note_asr_result(turn.lang if turn is not None else "", False)
            await self._emit_ui(
                "error",
                {"code": "asr_error", "message": event.error or "", "recoverable": True},
                turn_id,
            )
            return
        if turn is None or not event.text.strip():
            return

        previous = self.language.current
        decision = self.language.observe(event.text, event.lang)
        turn.user_text = event.text
        turn.lang = decision.lang
        turn.asr_driver = decision.asr_driver
        self._apply_voice(turn, decision.voice)
        self.language.note_asr_result(decision.lang, True)
        elapsed_ms = (self._clock() - turn.started_at) * 1000.0
        self.metrics.record(ASR_FINAL, elapsed_ms)
        await self._emit_ui(
            "asr.final",
            {
                "text": event.text,
                "duration_ms": round(elapsed_ms, 2),
                "lang": event.lang or turn.lang,
            },
            turn_id,
        )
        if decision.changed:
            await self._emit_ui(
                "language.changed",
                {
                    "from": previous,
                    "to": decision.lang,
                    "mode": self.language.mode,
                    "voice": decision.voice.as_dict(),
                },
                turn_id,
            )
        await self._handle(Event(EventType.EV_ASR_FINAL, turn_id, self.session_id), turn)
        self._start_turn_task(turn)

    async def _close_asr(self) -> None:
        # 先取流再取消消费任务：消费任务退出时会清空 `_asr_stream`，
        # 若放到后面读取会漏掉 `close()`，导致 ASR 连接未真正释放（C-144）。
        stream = self._asr_stream
        self._asr_stream = None
        task = self._asr_task
        self._asr_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.close()

    # ------------------------------------------------------------ 打断（T-4.1 ~ T-4.6）

    async def _enqueue_sentence(
        self, turn: DialogTurn, queue: asyncio.Queue[str | None], sentence: str
    ) -> None:
        """句子入播放队列，并登记为「已送入播放队列」的文本（FR-6.8 / T-4.5）。"""
        await queue.put(sentence)
        turn.spoken_fragments.append(sentence)
        self.barge.note_spoken_text(sentence)

    def _schedule_barge_asr(self) -> None:
        """异步拉起播报期 ASR 热流，避免阻塞采集链路。"""
        if not self.barge.enabled or self._asr_stream is not None:
            return
        if self._barge_asr_task is not None and not self._barge_asr_task.done():
            return
        self._barge_asr_task = asyncio.create_task(
            self._ensure_barge_asr(), name="barge-asr"
        )

    async def _ensure_barge_asr(self) -> None:
        """播报期保证 ASR 热流常开：partial 用于语义确认，打断后直接复用（T-4.4）。"""
        if self._asr_stream is not None or not self.barge.enabled:
            return
        if self.machine.state not in (
            State.SPEAKING,
            State.INTERRUPTING,
            State.LISTENING,
        ):
            return
        await self._open_asr(preopen=True)

    async def _on_asr_during_playback(self, event: ASREvent, turn_id: int | None) -> None:
        """播报期 ASR 结果只用于语义回声剔除，不写当前轮次（T-4.2 / C-07）。"""
        if event.type == "error":
            self.error_count += 1
            logger.warning("barge_asr_error", detail=event.error or "")
            return
        text = event.text.strip()
        if not text:
            return
        self._maybe_start_barge_in(self.barge.observe_partial(text))

    def _maybe_start_barge_in(self, decision: BargeInDecision | None) -> None:
        """统一处理检测结论：回声丢弃仅计数，确认成立才异步执行打断序列。"""
        if decision is None:
            return
        if self._confirming:
            # 确认问题播报期间禁止 Barge-in，避免打断确认流程（FR-9.10）
            return
        if decision.echo_suppressed:
            self.metrics.increment(BARGE_IN_ECHO_SUPPRESSED)
            logger.info(
                "barge_in_echo_suppressed",
                reason=decision.reason,
                similarity_text=decision.text,
            )
            return
        if not decision.accepted:
            return
        if self.machine.state not in (State.THINKING, State.SPEAKING, State.EXECUTING):
            return
        if self._barge_task is not None and not self._barge_task.done():
            return
        self.metrics.increment(BARGE_IN_ACCEPTED)
        self._barge_task = asyncio.create_task(
            self._on_barge_in(decision), name="barge-in"
        )

    async def _on_barge_in(self, decision: BargeInDecision) -> None:
        """打断执行序列（design §6.3）：先停声、再清理、后恢复识别，各阶段埋点。"""
        started = self._clock()
        stages: dict[str, float] = {}
        turn = self._current_turn
        turn_id = None if turn is None else turn.turn_id
        self.context.barge_in_accepted = True
        try:
            await self._handle(
                Event(EventType.EV_BARGE_IN, turn_id, self.session_id), turn
            )
            stages["state"] = _ms(self._clock() - started)
            # ① 优先级最高：立即停声并清空待播队列与音频缓冲（含 ④ 清队列）
            if self._pipeline is not None:
                with contextlib.suppress(Exception):
                    await self._pipeline.flush()
            self.barge.mark_playback_stop()
            stages["flush"] = _ms(self._clock() - started)
            # ② 当前轮次标记 interrupted；③ 取消令牌中止 LLM / TTS 流
            if turn is not None:
                turn.status = "interrupted"
            await self._cancel_turn("barge_in")
            stages["cancel"] = _ms(self._clock() - started)
            # ⑤ 归档轮次：仅已送入播放队列的文本进入记忆（T-4.5 / FR-6.8）
            if turn is not None:
                self._finish_turn(turn)
            stages["archive"] = _ms(self._clock() - started)
            # ② 递增 turn_id：新轮次承接打断后的话语，陈旧事件自动失效
            if self._running:
                self._begin_turn("")
            stages["turn"] = _ms(self._clock() - started)
            # ⑦ 复用热 ASR 会话；无热流时重新打开（T-4.4）
            if self._asr_stream is None and self._running:
                await self._open_asr()
            stages["resume"] = _ms(self._clock() - started)
        finally:
            self.context.barge_in_accepted = False
        if turn is not None:
            turn.barge_in_stages = stages
        total = _ms(self._clock() - started)
        payload: dict[str, Any] = decision.as_dict()
        payload.update(
            {
                "session_id": self.session_id,
                "turn_id": turn_id,
                "next_turn_id": self.turns.current_turn_id,
                "from_state": State.INTERRUPTING.value,
                "stage_latency": stages,
                "total_ms": total,
                "result": "accepted",
            }
        )
        self._last_barge = payload
        self.metrics.record(BARGE_IN, total)
        await self._emit_ui("barge_in.accepted", payload, turn_id)
        await self._handle(
            Event(EventType.EV_INTERRUPT_DONE, turn_id, self.session_id), None
        )

    async def _cancel_barge_tasks(self) -> None:
        """收敛打断相关任务，避免复位 / 关停后残留写库或重连 ASR。"""
        for task in (self._barge_task, self._barge_asr_task):
            if task is not None and not task.done():
                await _cancel_task(task)
        self._barge_task = None
        self._barge_asr_task = None

    def barge_in_snapshot(self) -> dict[str, Any]:
        """打断参数、保护窗口、计数器与最近一次打断阶段耗时（C-10 / C-57）。"""
        snapshot: dict[str, Any] = dict(self.barge.snapshot())
        snapshot["state"] = self.machine.state.value
        snapshot["metrics"] = {
            "accepted": self.metrics.counter(BARGE_IN_ACCEPTED),
            "echo_suppressed": self.metrics.counter(BARGE_IN_ECHO_SUPPRESSED),
            "rejected": self.metrics.counter(BARGE_IN_REJECTED),
        }
        snapshot["last"] = self._last_barge
        return snapshot

    async def set_barge_in(self, params: dict[str, Any]) -> dict[str, Any]:
        """运行时热更新打断参数（T-4.6）：校验通过后立即生效，无需重启。"""
        merged = {**self.settings.barge_in.model_dump(), **params}
        settings = BargeInSettings.model_validate(merged)
        self.settings.barge_in = settings
        self.barge.apply_settings(settings)
        if not self.barge.enabled:
            # 半双工兜底：立即解除播报窗口并拒绝残留待确认（T-4.8）
            self.barge.reject("disabled")
            self.barge.mark_playback_stop()
        snapshot = self.barge_in_snapshot()
        await self._emit_ui("barge_in.updated", snapshot, None)
        logger.info("barge_in_updated", **settings.model_dump())
        return snapshot

    # ------------------------------------------------------------ 唤醒与待机（T-3A.*）

    def _detect_cloud_asr(self) -> bool:
        """常开聆听时用于判断 ASR 是否为云端驱动（FR-14.8 / CON-5）。"""
        asr = self.settings.providers.asr
        active = asr.drivers.get(asr.active, {})
        return bool(active.get("cloud", False))

    async def _sync_wake_state(self, state: State) -> None:
        """按状态启停 KWS 与活跃窗口（INV-9：KWS 只在 `IDLE` 运行）。"""
        if not self.context.wakeword_enabled:
            self._cancel_active_window()
            await self._stop_kws()
            return
        if state == State.IDLE:
            self._cancel_active_window()
            await self._start_kws()
            return
        await self._stop_kws()
        if state == State.ACTIVE_IDLE:
            self.refresh_active_window()
        else:
            self._cancel_active_window()

    async def _start_kws(self) -> None:
        if self._kws is None and self._kws_error:
            return
        if self._kws is not None:
            if not self._kws.running and not self._kws_error:
                await self._kws.start()
                if self._kws.running:
                    self._kws_error = ""
            return
        wakeword = self.settings.providers.wakeword
        provider = self._providers.get("kws")
        if provider is None:
            config = dict(wakeword.drivers.get(wakeword.active, {}))
            config["confirm_twice"] = wakeword.confirm_twice
            try:
                provider = self.registry.create_active("kws", wakeword.active, config)
            except Exception as exc:
                self._kws_error = repr(exc)
                logger.warning("kws_driver_unavailable", detail=self._kws_error)
                return
        self._kws = KwsScheduler(
            provider,
            on_wake=self._on_wake_detected,
            is_standby=lambda: self.machine.state == State.IDLE,
            metrics=self.metrics,
            clock=self._clock,
        )
        await self._kws.start()
        if not self._kws.running:
            self._kws_error = self._kws.error
            logger.warning("kws_unavailable", detail=self._kws_error)

    async def _stop_kws(self) -> None:
        if self._kws is not None:
            await self._kws.stop()

    async def _on_wake_detected(self, event: WakeEvent) -> None:
        """唤醒闭环：`IDLE → WAKING → ACTIVE_IDLE` + 反馈 + ASR 预连接（T-3A.4 / T-3A.5）。"""
        if self.machine.state != State.IDLE or self._current_turn is not None:
            return
        wakeword = self.settings.providers.wakeword
        started = self._clock()
        self.watchdog.mark_progress("kws")
        await self._emit_ui(
            "wake.detected",
            {"phrase": event.phrase, "confidence": round(event.confidence, 4)},
            None,
        )
        await self._handle(Event(EventType.EV_WAKE_DETECTED, session_id=self.session_id), None)
        if wakeword.preopen_asr:
            await self._open_asr(preopen=True)
        await self._play_wake_feedback()
        self.metrics.record(WAKE_LATENCY, (self._clock() - started) * 1000.0)
        await self._handle(Event(EventType.EV_WAKE_ACK_DONE, session_id=self.session_id), None)
        await self._flush_standby_results()

    async def _play_wake_feedback(self) -> None:
        feedback = self.settings.providers.wakeword.feedback
        if feedback.mode == "none":
            return
        if feedback.mode == "beep":
            await self._play_beep()
            return
        tts = await self._acquire("tts")
        if tts is None:
            await self._play_beep()
            return
        lang = self.language.current
        sample_rate = int(
            getattr(getattr(tts, "capabilities", None), "sample_rate", 16000) or 16000
        )
        token = self._root_token.child("wake-feedback")
        request = TTSRequest(
            text=feedback.text, voice="default", sample_rate=sample_rate, lang=lang
        )
        try:
            async for chunk in tts.synthesize_stream(request, token):
                if self._pipeline is not None:
                    await self._pipeline.play(chunk)
            if self._pipeline is not None:
                await self._pipeline.wait_until_idle(timeout_s=0.6)
        except Exception:
            logger.exception("wake_feedback_failed")
            await self._play_beep()

    async def _play_beep(self) -> None:
        if self._pipeline is None:
            return
        chunks = self._beep_chunks()
        if not chunks:
            return
        for chunk in chunks:
            await self._pipeline.play(chunk)
        await self._pipeline.wait_until_idle(timeout_s=0.6)

    def _beep_chunks(self) -> list[AudioChunk]:
        if self._beep_cache is not None:
            return self._beep_cache
        path = Path(self.settings.providers.wakeword.feedback.beep_path)
        if path.exists():
            self._beep_cache = _load_wav_chunks(path)
        else:
            # 资产缺失时用等时长合成提示音兜底，保证唤醒反馈可感知（FR-14.3）
            self._beep_cache = [_tone_chunk()]
        return self._beep_cache

    # ------------------------------------------------------------ 活跃窗口

    def refresh_active_window(self) -> None:
        """刷新活跃窗口（唤醒、文本输入、每轮播报完成、任务发起 / 完成）。"""
        self._cancel_active_window()
        if not self.context.wakeword_enabled:
            return
        if self.machine.state != State.ACTIVE_IDLE:
            return
        window_ms = self.settings.providers.wakeword.active_window_ms
        self._active_window_deadline = self._clock() + window_ms / 1000.0
        self._active_window_task = asyncio.create_task(
            self._active_window_loop(window_ms), name="active-window"
        )

    def _cancel_active_window(self) -> None:
        self._active_window_deadline = None
        task = self._active_window_task
        self._active_window_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _active_window_loop(self, window_ms: int) -> None:
        try:
            await asyncio.sleep(window_ms / 1000.0)
        except asyncio.CancelledError:
            raise
        if self.machine.state != State.ACTIVE_IDLE:
            return
        self.metrics.increment(ACTIVE_WINDOW_EXPIRED)
        self._active_window_deadline = None
        await self._emit_ui("session.active_timeout", {"window_ms": window_ms}, None)
        # 超时回待机并释放 ASR；KWS 由状态转移钩子恢复（FR-14.5 / C-144）
        await self._close_asr()
        await self._handle(
            Event(EventType.EV_SESSION_ACTIVE_TIMEOUT, session_id=self.session_id), None
        )

    # ------------------------------------------------------------ 长任务与待机结果（T-3A.9）

    def notify_task_started(self) -> None:
        """任务发起时刷新活跃窗口一次；执行期间不再刷新（FR-14.15）。"""
        self.refresh_active_window()

    async def announce_task_result(self, text: str, speak: bool | None = None) -> dict[str, Any]:
        """任务结果播报：活跃期直接播报；待机态默认缓存 + 提示音（FR-14.15 / FR-14.16）。"""
        text = text.strip()
        if not text:
            return {"announced": False, "cached": False, "reason": "empty"}
        if not self.context.wakeword_enabled:
            # 常开聆听：不存在待机态，直接按活跃期规则播报（FR-14.8）
            announced = await self._speak_text(text)
            self.refresh_active_window()
            return {"announced": announced, "cached": False, "reason": "open_ear"}

        standby = self.settings.providers.wakeword.standby
        if self.machine.state == State.IDLE:
            if speak is None:
                speak = standby.announce
            if speak:
                announced = await self._speak_text(text)
                return {"announced": announced, "cached": False, "reason": "standby_announce"}
            self._pending_standby.append(text)
            self.metrics.increment(STANDBY_RESULT_CACHED)
            if standby.announce_beep:
                await self._play_beep()
            await self._emit_ui("standby.result", {"cached": True, "text": text}, None)
            return {"announced": False, "cached": True, "reason": "standby_cached"}

        announced = await self._speak_text(text)
        self.refresh_active_window()
        await self._emit_ui("task.result", {"text": text, "announced": announced}, None)
        return {"announced": announced, "cached": False, "reason": "active"}

    async def _flush_standby_results(self) -> None:
        if not self._pending_standby:
            self.refresh_active_window()
            return
        while self._pending_standby:
            text = self._pending_standby.popleft()
            await self._speak_text(text)
        self.refresh_active_window()

    async def _speak_text(self, text: str, *, timeout_s: float = 30.0) -> bool:
        """不经过状态机的纯播报（待机结果 / 唤醒反馈），不拉起 ASR。"""
        if self._pipeline is None:
            return False
        tts = await self._acquire("tts")
        if tts is None:
            return False
        lang = self.language.current
        sample_rate = int(
            getattr(getattr(tts, "capabilities", None), "sample_rate", 16000) or 16000
        )
        token = self._root_token.child("announce")
        request = TTSRequest(
            text=text, voice="default", sample_rate=sample_rate, lang=lang
        )
        try:
            async for chunk in tts.synthesize_stream(request, token):
                if self._pipeline is None or token.cancelled:
                    return False
                await self._pipeline.play(chunk)
        except Exception:
            logger.exception("announce_synthesis_failed")
            return False
        await self._pipeline.wait_until_idle(timeout_s=timeout_s)
        await self._emit_ui("playback.state", {"playing": False, "queued_ms": 0}, None)
        return True

    def mark_false_wake(self, reason: str = "operator") -> int:
        """运维标记一次误唤醒（FR-14.13 / C-148），只上报计数不携带音频。"""
        self.metrics.increment(WAKE_FALSE_POSITIVE)
        logger.info("wake_false_positive", reason=reason)
        return self.metrics.counter(WAKE_FALSE_POSITIVE)

    def wake_snapshot(self) -> dict[str, Any]:
        wakeword = self.settings.providers.wakeword
        remaining: float | None = None
        if self._active_window_deadline is not None:
            remaining = round(
                max(0.0, self._active_window_deadline - self._clock()), 3
            )
        return {
            "enabled": wakeword.enabled,
            "state": self.machine.state.value,
            "active_window_ms": wakeword.active_window_ms,
            "active_window_remaining_s": remaining,
            "preopen_asr": wakeword.preopen_asr,
            "confirm_twice": wakeword.confirm_twice,
            "feedback": wakeword.feedback.model_dump(),
            "standby": wakeword.standby.model_dump(),
            "open_ear": {
                "asr_cloud": self._asr_cloud,
                "cloud_asr_ack": wakeword.open_ear.cloud_asr_ack,
            },
            "kws": None if self._kws is None else self._kws.snapshot(),
            "kws_error": self._kws_error,
            "pending_standby_results": len(self._pending_standby),
            "counters": {
                "wake_count": self.metrics.counter("wake_count"),
                "wake_false_positive": self.metrics.counter("wake_false_positive"),
                "active_window_expired": self.metrics.counter("active_window_expired"),
            },
        }

    # ------------------------------------------------------------ 轮次执行

    def _begin_turn(self, user_text: str) -> DialogTurn:
        lang = self.language.current
        turn_id = self.turns.new_turn()
        db_id = self._store.start_turn(self.session_id, turn_id, lang=lang, user_text=user_text)
        driver, _ = self.language.route(lang)
        turn = DialogTurn(
            turn_id=turn_id,
            db_id=db_id,
            lang=lang,
            user_text=user_text,
            asr_driver=driver,
        )
        self._apply_voice(turn, self.language.voice_plan(lang))
        self._current_turn = turn
        self.turn_count += 1
        return turn

    @staticmethod
    def _apply_voice(turn: DialogTurn, plan: VoicePlan) -> None:
        turn.tts_driver = plan.driver
        turn.tts_voice = plan.voice
        turn.tts_lang = plan.tts_lang
        turn.tts_fallback = plan.tts_fallback
        turn.tts_noted = False

    def _start_turn_task(self, turn: DialogTurn) -> None:
        if self._turn_task is not None and not self._turn_task.done():
            return
        self._turn_task = asyncio.create_task(
            self._run_turn(turn), name=f"dialog-turn-{turn.turn_id}"
        )

    async def _run_turn(self, turn: DialogTurn) -> None:
        token = self._root_token.child(f"turn-{turn.turn_id}")
        self._turn_token = token
        try:
            await self._speak_turn(turn, token)
            if turn.status == "active":
                turn.status = "completed"
        except asyncio.CancelledError:
            # 打断已把状态置为 interrupted，取消不得覆盖（T-4.5 / C-72）
            if turn.status == "active":
                turn.status = "aborted"
            raise
        except Exception as exc:
            self.error_count += 1
            turn.status = "failed"
            logger.exception("dialog_turn_failed", turn_id=turn.turn_id)
            await self._emit_ui(
                "error",
                {"code": "turn_failed", "message": repr(exc), "recoverable": True},
                turn.turn_id,
            )
            await self._recover_after_failure()
        finally:
            self._finish_turn(turn)
            if self._turn_token is token:
                self._turn_token = None

    async def _speak_turn(self, turn: DialogTurn, token: CancellationToken) -> None:
        llm = await self._acquire("llm")
        if llm is None:
            raise RuntimeError("LLM 驱动不可用")

        rag_outcome = RagOutcome(reason="disabled")
        if self.rag is not None:
            rag_outcome = await self.rag.retrieve(turn.user_text, token)
            turn.rag_hit_ids = rag_outcome.hit_ids
            # 结构化命中记录（doc_id / score / skipped）写入轮次，供追溯（T-5.12 / C-176）
            turn.rag_hits = rag_outcome.records

        if turn.tts_fallback:
            # 方言无音色：音色降级为普通话，提示词同步约束为普通话书面语（FR-15.14）
            self._note_tts_fallback(turn)
            await self._emit_ui(
                "language.tts_fallback",
                {"lang": turn.lang, "tts_lang": turn.tts_lang or turn.lang},
                turn.turn_id,
            )

        tools = self._request_tools(llm)
        context = self._build_messages(turn, rag_outcome, llm=llm, tools_available=bool(tools))
        await self._refresh_summary(context.dropped, turn)
        request = ChatRequest(
            messages=context.messages,
            tools=tools,
            lang=turn.lang,
            session_id=self.session_id,
            turn_id=turn.turn_id,
        )

        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=16)
        tts_task = asyncio.create_task(
            self._tts_consumer(turn, token, queue),
            name=f"tts-consumer-{turn.turn_id}",
        )
        splitter = SentenceSplitter()
        started = self._clock()
        first_token = False

        try:
            rounds = 0
            while True:
                first_token, calls = await self._stream_llm_round(
                    turn, token, llm, request, splitter, queue, tts_task, started, first_token
                )
                if not calls or not self.context.tools_authorized:
                    break
                if rounds >= self.settings.tools.max_rounds:
                    logger.warning(
                        "tool_rounds_exceeded", turn_id=turn.turn_id, rounds=rounds
                    )
                    break
                rounds += 1
                tool_messages = await self._execute_tool_calls(turn, token, calls)
                if token.cancelled or not tool_messages:
                    break
                # 结果回灌 LLM：以 user 消息承载工具输出（避免 tool_call_id 协议耦合）
                request = ChatRequest(
                    messages=[*request.messages, *tool_messages],
                    tools=tools,
                    lang=turn.lang,
                    session_id=self.session_id,
                    turn_id=turn.turn_id,
                )

            for sentence in splitter.flush():
                if token.cancelled:
                    break
                if tts_task.done():
                    await tts_task
                await self._enqueue_sentence(turn, queue, sentence)
            await queue.put(None)
            await tts_task
        except asyncio.CancelledError:
            await _cancel_task(tts_task)
            raise
        except Exception:
            await _cancel_task(tts_task)
            raise

        if not first_token and self.machine.state == State.THINKING:
            # 工具轮次可能没有任何文本输出：补首 token 事件，避免状态机停在 THINKING
            await self._handle(
                Event(EventType.EV_LLM_FIRST_TOKEN, turn.turn_id, self.session_id), turn
            )
        if self._pipeline is None:
            return
        drained = await self._pipeline.wait_until_idle(timeout_s=30.0)
        self.barge.mark_playback_stop()
        if not drained:
            await self._pipeline.flush()
        await self._emit_ui(
            "playback.state", {"playing": False, "queued_ms": 0}, turn.turn_id
        )
        await self._handle(
            Event(EventType.EV_PLAYBACK_DONE, turn.turn_id, self.session_id), turn
        )

    # ------------------------------------------------------------ 工具派发（T-5.5 ~ T-5.11）

    async def _stream_llm_round(
        self,
        turn: DialogTurn,
        token: CancellationToken,
        llm: Any,
        request: ChatRequest,
        splitter: SentenceSplitter,
        queue: asyncio.Queue[str | None],
        tts_task: asyncio.Task[Any],
        started: float,
        first_token: bool,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """单轮 LLM 流式读取：边收 token 边切句送 TTS，同时收集 tool_call。"""
        calls: list[dict[str, Any]] = []
        async for event in llm.chat_stream(request, token):
            if token.cancelled:
                break
            if event.type == "token":
                if not first_token:
                    first_token = True
                    turn.timings["llm_first"] = _ms(self._clock() - started)
                    self.metrics.record(LLM_FIRST_TOKEN, turn.timings["llm_first"])
                    await self._handle(
                        Event(EventType.EV_LLM_FIRST_TOKEN, turn.turn_id, self.session_id),
                        turn,
                    )
                turn.assistant_text += event.text
                await self._emit_ui("llm.delta", {"text": event.text}, turn.turn_id)
                for sentence in splitter.push(event.text):
                    if token.cancelled:
                        break
                    if tts_task.done():
                        await tts_task  # TTS 不可用时立即上报，避免队列堵死
                    await self._enqueue_sentence(turn, queue, sentence)
            elif event.type == "tool_call":
                payload = dict(event.tool_call or {})
                if payload.get("name"):
                    calls.append(payload)
            elif event.type == "error":
                raise RuntimeError(event.error or "LLM 流错误")
            elif event.type == "done":
                break
        return first_token, calls

    def _request_tools(self, llm: Any) -> list[ToolDefinition]:
        """能力协商后决定是否把工具暴露给 LLM（C-58 / T-5.5）。"""
        effective = negotiate_llm_capabilities(llm, self.tools is not None)
        self.context.tools_authorized = effective.tools_enabled
        if self.tools is None or not effective.tools_enabled:
            return []
        return self.tools.enabled_definitions()

    async def _execute_tool_calls(
        self, turn: DialogTurn, token: CancellationToken, calls: list[dict[str, Any]]
    ) -> list[ChatMessage]:
        """逐个执行工具：策略 → 二次确认 → 异步 / 同步 → 结果回灌消息。"""
        if self.tools is None:
            return []
        messages: list[ChatMessage] = []
        for index, raw in enumerate(calls):
            name = str(raw.get("name") or "")
            raw_args = raw.get("arguments")
            args = dict(raw_args) if isinstance(raw_args, dict) else {}
            call = ToolCall(
                id=str(raw.get("id") or f"{turn.turn_id}-{name}-{index}"),
                name=name,
                args=args,
                turn_id=turn.turn_id,
            )
            await self._emit_ui(
                "llm.tool_call",
                {"id": call.id, "name": call.name, "args": call.args},
                turn.turn_id,
            )
            definition, decision = self.tools.check(call)
            if definition is None or not decision.allowed:
                result = ToolResult(
                    ok=False,
                    error=decision.reason or f"未知工具 {name}",
                    error_kind="denied",
                )
                await self._record_tool_result(turn, call, result)
                messages.append(ChatMessage(role="user", content=self._tool_message(call, result)))
                continue
            if decision.needs_confirm and not await self._confirm_tool(turn, call, definition):
                result = ToolResult(
                    ok=False,
                    error="未确认或确认超时，已取消执行",
                    error_kind="cancelled",
                )
                await self._record_tool_result(turn, call, result)
                messages.append(ChatMessage(role="user", content=self._tool_message(call, result)))
                continue
            if definition.timeout_s > self.settings.tools.async_threshold_s:
                # 长任务移交执行层，对话继续（T-5.9 / C-83）
                self._dispatch_async_tool(turn, call)
                messages.append(
                    ChatMessage(
                        role="user",
                        content=(
                            f"[工具结果] {call.name} 已在后台执行，完成后会主动播报结果；"
                            "请先给用户简短确认。"
                        ),
                    )
                )
                continue
            if self.machine.state == State.THINKING and self.context.tools_authorized:
                await self._handle(
                    Event(EventType.EV_LLM_TOOL_CALL, turn.turn_id, self.session_id), turn
                )
            started = self._clock()
            result = await self.tools.invoke(call, None, turn_id=turn.turn_id, checked=True)
            self.metrics.record(TOOL_EXECUTION, _ms(self._clock() - started))
            if self.machine.state == State.EXECUTING:
                event_type = (
                    EventType.EV_TOOL_TIMEOUT
                    if result.error_kind == "timeout"
                    else EventType.EV_TOOL_ERROR
                    if not result.ok
                    else EventType.EV_TOOL_DONE
                )
                await self._handle(Event(event_type, turn.turn_id, self.session_id), turn)
            await self._record_tool_result(turn, call, result)
            messages.append(ChatMessage(role="user", content=self._tool_message(call, result)))
        return messages

    @staticmethod
    def _tool_message(call: ToolCall, result: ToolResult) -> str:
        if result.ok:
            return f"[工具结果] {call.name} {result.output or '执行成功'}"
        return (
            f"[工具结果] {call.name} 执行失败：{result.error or '未知错误'}。"
            "请用自然语言简要告知用户。"
        )

    async def _record_tool_result(
        self, turn: DialogTurn, call: ToolCall, result: ToolResult
    ) -> None:
        record = {"id": call.id, "name": call.name, "args": call.args, **result.as_dict()}
        turn.tool_calls.append(record)
        await self._emit_ui("tool.result", record, turn.turn_id)

    def _dispatch_async_tool(self, turn: DialogTurn, call: ToolCall) -> None:
        self.notify_task_started()
        self.dispatch_background(
            self._run_async_tool(turn, call), name=f"tool-{call.name}-{turn.turn_id}"
        )
        logger.info("tool_dispatched_background", tool=call.name, turn_id=turn.turn_id)

    async def _run_async_tool(self, turn: DialogTurn, call: ToolCall) -> None:
        """后台长任务：不占用对话链路，完成后主动播报（FR-14.15）。"""
        if self.tools is None:
            return
        started = self._clock()
        result = await self.tools.invoke(call, None, turn_id=turn.turn_id, checked=True)
        self.metrics.record(TOOL_EXECUTION, _ms(self._clock() - started))
        await self._record_tool_result(turn, call, result)
        await self.announce_task_result(self._tool_message(call, result))

    async def _confirm_tool(
        self, turn: DialogTurn, call: ToolCall, definition: ToolDefinition
    ) -> bool:
        """高风险二次确认：播报问题期间不启用 Barge-in，语音只作确认判定（FR-9.10）。"""
        if self.tools is None:
            return False
        policy = self.tools.policy
        question = self._confirm_question(call, definition)
        self._confirming = True
        self._confirm_future = asyncio.get_running_loop().create_future()
        await self._emit_ui(
            "tool.confirm",
            {
                "tool": call.name,
                "args": call.args,
                "question": question,
                "timeout_ms": policy.confirm_timeout_ms,
            },
            turn.turn_id,
        )
        if self._asr_stream is None:
            await self._open_asr(preopen=True)
        announce = asyncio.create_task(
            self._speak_text(question), name=f"tool-confirm-{turn.turn_id}"
        )
        confirmed = False
        reason = "timeout"
        try:
            confirmed = await asyncio.wait_for(
                self._confirm_future, timeout=policy.confirm_timeout_ms / 1000.0
            )
            reason = "user"
        except asyncio.TimeoutError:
            reason = "timeout"
        finally:
            self._confirming = False
            self._confirm_future = None
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await announce
        await self._emit_ui(
            "tool.confirm.resolved",
            {"tool": call.name, "confirmed": bool(confirmed), "reason": reason},
            turn.turn_id,
        )
        return bool(confirmed)

    @staticmethod
    def _confirm_question(call: ToolCall, definition: ToolDefinition) -> str:
        args = "，".join(f"{key} 为 {value}" for key, value in call.args.items())
        detail = f"，{args}" if args else ""
        return f"即将{definition.description}{detail}。请说确认或取消。"

    def _resolve_confirm(self, confirmed: bool) -> None:
        future = self._confirm_future
        if future is not None and not future.done():
            future.set_result(confirmed)

    async def _on_asr_during_confirm(self, event: ASREvent) -> None:
        """确认期 ASR 只做确认 / 取消判定，不新建轮次、不污染当前轮次。"""
        if event.type == "error":
            self.error_count += 1
            return
        if event.type != "final":
            return
        text = event.text.strip()
        if not text:
            return
        answer = self.tools.policy.decide_confirmation(text) if self.tools is not None else ""
        confirmed = answer == CONFIRM_ANSWER
        if not answer:
            # 无法判定按取消处理（FR-9.10），避免误执行高风险动作
            logger.info("tool_confirm_unrecognized", text=text)
        await self._emit_ui(
            "tool.confirm.heard",
            {"text": text, "answer": answer or "unknown", "confirmed": confirmed},
            self.turns.current_turn_id,
        )
        self._resolve_confirm(confirmed)

    async def _refresh_summary(
        self, dropped: list[ChatMessage], turn: DialogTurn
    ) -> None:
        """把滑窗裁掉的历史压进滚动摘要（T-5.3 / C-73）。"""
        if self.memory is None or not dropped:
            return
        with contextlib.suppress(Exception):
            self._summary = await self.memory.update_summary(
                self.session_id, dropped, turn.turn_id
            )

    async def _tts_consumer(
        self,
        turn: DialogTurn,
        token: CancellationToken,
        queue: asyncio.Queue[str | None],
    ) -> None:
        tts = await self._acquire_tts(turn.tts_driver)
        active_tts = self.settings.providers.tts.active
        if tts is None and turn.tts_driver and turn.tts_driver != active_tts:
            # 方言音色驱动不可用：回退主 TTS + 普通话音色并打降级标记（FR-15.14）
            tts = await self._acquire("tts")
            if tts is not None:
                fallback_plan = self.language.voice_plan(self.language.fallback)
                turn.tts_driver = active_tts
                turn.tts_voice = fallback_plan.voice
                turn.tts_lang = fallback_plan.tts_lang
                turn.tts_fallback = True
                self._note_tts_fallback(turn)
        if tts is None:
            raise RuntimeError("TTS 驱动不可用")
        first_chunk = True
        sample_rate = int(
            getattr(getattr(tts, "capabilities", None), "sample_rate", 16000)
            or self.settings.audio.capture.target_sample_rate
        )
        try:
            while True:
                sentence = await queue.get()
                if sentence is None or token.cancelled:
                    return
                request = TTSRequest(
                    text=sentence,
                    voice=turn.tts_voice,
                    sample_rate=sample_rate,
                    lang=turn.tts_lang or turn.lang,
                    turn_id=turn.turn_id,
                )
                async for chunk in tts.synthesize_stream(request, token):
                    if token.cancelled or self._pipeline is None:
                        return
                    if first_chunk:
                        first_chunk = False
                        turn.timings["tts_first"] = _ms(self._clock() - turn.started_at)
                        self.metrics.record(TTS_FIRST_CHUNK, turn.timings["tts_first"])
                        base = turn.speech_end_at or turn.started_at
                        turn.timings["e2e"] = _ms(self._clock() - base)
                        self.metrics.record(E2E_FIRST_RESPONSE, turn.timings["e2e"])
                        await self._handle(
                            Event(EventType.EV_TTS_FIRST_CHUNK, turn.turn_id, self.session_id),
                            turn,
                        )
                        # 首块即将出声：开启保护窗口并拉起 ASR 热流（T-4.1 / T-4.4）
                        self.barge.mark_playback_start()
                        self._schedule_barge_asr()
                    await self._pipeline.play(chunk)
                await self._handle(
                    Event(EventType.EV_TTS_DONE, turn.turn_id, self.session_id), turn
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.registry.record_named_failure("tts", turn.tts_driver)
            raise
        self.registry.record_named_success("tts", turn.tts_driver)

    def _note_tts_fallback(self, turn: DialogTurn) -> None:
        if turn.tts_noted:
            return
        turn.tts_noted = True
        self.language.note_tts_fallback(turn.lang)

    async def _acquire_tts(self, driver: str) -> Any | None:
        """按音色方案取 TTS 驱动：主驱动走槽位，方言音色驱动按名取用（T-2A.6）。"""
        if "tts" in self._providers:
            return self._providers["tts"]
        if not driver or driver == self.settings.providers.tts.active:
            return await self._acquire("tts")
        config = dict(self.settings.providers.tts.drivers.get(driver, {}))
        try:
            entry = await self.registry.acquire_named("tts", driver, config)
        except Exception as exc:
            logger.warning("named_tts_unavailable", driver=driver, detail=repr(exc))
            return None
        self.watchdog.mark_progress("provider_tts")
        return entry.driver

    def _finish_turn(self, turn: DialogTurn) -> None:
        # 打断序列会主动归档一次，`_run_turn` 的 finally 只做兜底，必须幂等
        if turn.finished:
            return
        turn.finished = True
        interrupted = turn.status == "interrupted"
        total_chars = len(turn.assistant_text)
        # 已送入播放队列的文本片段：打断时只归档这部分（T-4.5 / FR-6.8）
        spoken_text = "".join(turn.spoken_fragments)
        turn.spoken_ratio = (
            round(min(1.0, len(spoken_text) / total_chars), 4) if total_chars else 0.0
        )
        archived_text = spoken_text if interrupted else turn.assistant_text
        if interrupted:
            dropped_text = turn.assistant_text[len(spoken_text) :]
            if dropped_text:
                # 未播出内容只进事件日志，不进入对话记忆（FR-6.8 / C-72）
                self._store.add_event(
                    self.session_id,
                    "barge_in.dropped_text",
                    turn.turn_id,
                    {"text": dropped_text, "spoken_ratio": turn.spoken_ratio},
                )
        self._store.complete_turn(
            turn.db_id,
            assistant_text=archived_text,
            status=turn.status,
            timings=turn.timings,
            barge_in=interrupted,
            # 结构化命中记录优先，缺失时退回 doc_id 列表（C-176）
            rag_hits=turn.rag_hits or turn.rag_hit_ids,
            asr_partials=turn.asr_partials,
            user_text=turn.user_text,
            lang=turn.lang,
            tts_lang=turn.tts_lang or turn.lang,
            tts_fallback=turn.tts_fallback,
            spoken_ratio=turn.spoken_ratio,
        )
        if turn.user_text:
            self._history.append(ChatMessage(role="user", content=turn.user_text))
        if archived_text:
            self._history.append(ChatMessage(role="assistant", content=archived_text))
        if len(self._history) > self._history_limit:
            self._history = self._history[-self._history_limit :]
        # 落库到会话记忆（T-5.1）：容器重启后历史可恢复（C-70 / C-126）
        if self.memory is not None and (turn.user_text or archived_text):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                self.dispatch_background(
                    self.memory.append_turn(
                        self.session_id,
                        turn.turn_id,
                        user_text=turn.user_text,
                        assistant_text=archived_text,
                        status=turn.status,
                    ),
                    name=f"memory-turn-{turn.turn_id}",
                )
        if self._current_turn is turn:
            self._current_turn = None
        self.turns.end_turn(turn.status)

    async def _cancel_turn(self, reason: str) -> None:
        token = self._turn_token
        if token is not None:
            token.cancel(reason)
        task = self._turn_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(task, return_exceptions=True)
        self._turn_task = None
        if token is not None:
            await token.settle(timeout=0.5)

    # ------------------------------------------------------------ 看门狗钩子

    async def _on_state_timeout(self, state: State) -> None:
        await self._emit_ui(
            "error",
            {
                "code": "state_timeout",
                "message": f"{state.value} 超时",
                "recoverable": True,
            },
            self.turns.current_turn_id,
        )
        if state == State.LISTENING:
            await self._close_asr()
        await self._handle(
            Event(EventType.EV_TIMEOUT, self.turns.current_turn_id, self.session_id), None
        )
        # 超时丢弃的轮次没走到 `_run_turn`，必须显式收尾：否则 `_current_turn` 悬挂，
        # 常开聆听下后续每次起音都会被 `_on_speech_start` 的早退挡掉
        turn = self._current_turn
        if turn is not None and (self._turn_task is None or self._turn_task.done()):
            turn.status = "failed"
            self._finish_turn(turn)

    async def _on_stall(self, reason: str) -> None:
        logger.error("watchdog_recover", reason=reason)
        await self._emit_ui(
            "error",
            {"code": "watchdog_recovered", "message": reason, "recoverable": True},
            self.turns.current_turn_id,
        )
        # 只取消当前轮次对话链路，已移交执行层的后台任务保持运行（FR-7.7）
        await self._cancel_turn(reason)
        await self._cancel_barge_tasks()
        self.barge.mark_playback_stop()
        await self._close_asr()
        if self._pipeline is not None:
            with contextlib.suppress(Exception):
                await self._pipeline.flush()
        await self._handle(Event(EventType.EV_HEALTH_FAIL, session_id=self.session_id), None)
        await self._handle(Event(EventType.EV_RECOVERED, session_id=self.session_id), None)

    async def _recover_after_failure(self) -> None:
        if self.machine.state in (State.THINKING, State.SPEAKING, State.EXECUTING):
            await self._handle(Event(EventType.EV_TIMEOUT, session_id=self.session_id), None)
        if self.machine.state == State.DEGRADED:
            await self._handle(Event(EventType.EV_HEALTH_OK, session_id=self.session_id), None)

    # ------------------------------------------------------------ 辅助

    async def _handle(self, event: Event, turn: DialogTurn | None) -> Any:
        del turn
        self.watchdog.mark_progress("event")
        record = await self.machine.handle(event)
        if record is not None:
            await self._emit_ui("state.changed", record.as_dict(), event.turn_id)
            await self._sync_wake_state(record.to_state)
        return record

    async def _emit_ui(
        self, kind: str, payload: dict[str, Any], turn_id: int | None
    ) -> None:
        with contextlib.suppress(Exception):
            self._store.add_event(self.session_id, kind, turn_id, payload)
        for sink in tuple(self._ui_sinks):
            try:
                result = sink(kind, payload, turn_id)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("ui_sink_failed", event_type=kind)

    async def _acquire(self, kind: str) -> Any | None:
        if kind in self._providers:
            self.watchdog.mark_progress(f"provider_{kind}")
            return self._providers[kind]
        try:
            entry = await self.registry.acquire(kind)
        except Exception as exc:
            self.registry.record_failure(kind, repr(exc))
            logger.warning("provider_acquire_failed", kind=kind, detail=repr(exc))
            return None
        self.registry.record_success(kind)
        self.watchdog.mark_progress(f"provider_{kind}")
        return entry.driver

    def _build_messages(
        self,
        turn: DialogTurn,
        outcome: RagOutcome,
        *,
        llm: Any = None,
        tools_available: bool = False,
    ) -> ContextResult:
        """按 token 预算组装上下文：系统提示词 + 摘要 + 知识片段 + 滑窗历史（T-5.2）。"""
        rag_context = ""
        if self.rag is not None:
            rag_context = self.rag.build_context(outcome)
        system = build_system_prompt(
            turn.lang,
            tts_lang=turn.tts_lang or None,
            tts_fallback=turn.tts_fallback,
            rag_context=rag_context,
            tools_available=tools_available,
        )
        return assemble_context(
            system=system,
            history=self._history,
            user_text=turn.user_text,
            budget_tokens=self._context_budget(llm),
            summary=self._summary,
        )

    def _context_budget(self, llm: Any) -> int:
        caps = getattr(llm, "capabilities", None)
        window = int(getattr(caps, "context_window", 8192) or 8192)
        ratio = float(self.settings.memory.context_budget_ratio)
        return max(64, int(window * ratio))

    # ------------------------------------------------------------ 语言开关（T-2A.7）

    async def set_language(
        self, mode: str | None = None, lang: str | None = None
    ) -> dict[str, Any]:
        """运维切换 fixed / auto 或直接切语言，并广播 `language.changed`。"""
        self.language.set_mode(mode, lang)
        payload = {
            "mode": self.language.mode,
            "lang": self.language.current,
            "source": "runtime",
        }
        await self._emit_ui("language.changed", payload, None)
        logger.info(
            "language_switched_by_api",
            mode=self.language.mode,
            lang=self.language.current,
        )
        return self.language.snapshot()

    async def reload_language(self) -> dict[str, Any]:
        """按最新 `language` 配置重建语言路由（配置热更新用，T-6.3）。"""
        self.language = LanguageRouter.from_settings(
            self.settings,
            driver_languages=lambda name: self.registry.driver_languages("asr", name),
            prepare=self._prewarm_driver,
        )
        snapshot = self.language.snapshot()
        await self._emit_ui(
            "language.changed",
            {"mode": snapshot["mode"], "lang": snapshot["current"], "source": "config"},
            None,
        )
        return snapshot

    def _prewarm_driver(self, driver: str) -> None:
        """防抖期间预连接目标 ASR 驱动，跨驱动切换后首响不劣化（FR-15.4）。"""
        if "asr" in self._providers:
            return
        if driver == self.settings.providers.asr.active:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._warm_named_asr(driver), name=f"language-prewarm-{driver}")

    async def _warm_named_asr(self, driver: str) -> None:
        config = dict(self.settings.providers.asr.drivers.get(driver, {}))
        try:
            entry = await self.registry.acquire_named("asr", driver, config)
            warmup = getattr(entry.driver, "warmup", None)
            if warmup is not None:
                await warmup()
            logger.info("language_driver_prewarmed", driver=driver)
        except Exception as exc:
            logger.warning("language_driver_prewarm_failed", driver=driver, detail=repr(exc))

    def tools_snapshot(self) -> dict[str, Any]:
        """工具清单、策略与调用统计（C-80 / T-5.4）。"""
        if self.tools is None:
            return {
                "enabled": False,
                "definitions": [],
                "enabled_tools": [],
                "adapters": [],
                "policy": {},
                "calls": 0,
                "failures": 0,
                "timeouts": 0,
                "denied": 0,
                "recent": [],
            }
        snapshot = self.tools.snapshot()
        # 注册表的 `enabled` 是工具名列表，避免与「工具能力是否启用」同名冲突
        enabled_tools = snapshot.pop("enabled", [])
        return {"enabled": True, "enabled_tools": enabled_tools, **snapshot}

    def memory_snapshot(self) -> dict[str, Any]:
        """记忆驱动状态与滑窗 / 摘要规模（T-5.1 / T-5.3）。"""
        if self.memory is None:
            return {
                "backend": "none",
                "history": len(self._history),
                "summary_chars": len(self._summary),
            }
        snapshot = dict(self.memory.snapshot())
        snapshot["history"] = len(self._history)
        snapshot["history_limit"] = self._history_limit
        snapshot["summary_chars"] = len(self._summary)
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state": self.machine.state.value,
            "running": self._running,
            "turns": self.turns.snapshot(),
            "watchdog": self.watchdog.snapshot(),
            "bus": self.bus.snapshot(),
            "background_tasks": len(self._background_tasks),
            "errors": self.error_count,
            "language": self.language.snapshot(),
            "wake": self.wake_snapshot(),
            "barge_in": self.barge_in_snapshot(),
            "memory": self.memory_snapshot(),
            "tools": self.tools_snapshot(),
        }


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 2)


def _tone_chunk(
    freq_hz: float = 880.0,
    duration_ms: float = 200.0,
    sample_rate: int = 16000,
    amplitude: float = 0.3,
) -> AudioChunk:
    """唤醒提示音兜底（资产缺失时保证反馈可感知，FR-14.3）。"""
    total = max(1, int(sample_rate * duration_ms / 1000.0))
    t = np.arange(total, dtype=np.float64) / sample_rate
    wave_data = amplitude * np.sin(2 * np.pi * freq_hz * t)
    return AudioChunk(data=float_to_pcm16(wave_data), sample_rate=sample_rate, channels=1)


def _load_wav_chunks(path: Path, frame_ms: int = 20) -> list[AudioChunk]:
    """读取 16bit WAV 提示音并按帧切分；格式不支持时返回空（调用方退化为合成音）。"""
    try:
        with wave.open(str(path), "rb") as reader:
            if reader.getsampwidth() != 2:
                return []
            sample_rate = reader.getframerate()
            channels = reader.getnchannels()
            samples_per_frame = max(1, int(sample_rate * frame_ms / 1000))
            chunks: list[AudioChunk] = []
            while True:
                data = reader.readframes(samples_per_frame)
                if not data:
                    break
                chunks.append(
                    AudioChunk(data=data, sample_rate=sample_rate, channels=channels)
                )
            return chunks
    except Exception:
        logger.warning("wake_beep_asset_invalid", path=str(path))
        return []


async def _cancel_task(task: asyncio.Task[Any]) -> None:
    if task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(task, return_exceptions=True)


__all__ = [
    "EffectiveCapabilities",
    "negotiate_llm_capabilities",
    "build_provider_report",
    "SentenceSplitter",
    "DialogTurn",
    "DialogOrchestrator",
    "SYSTEM_PROMPT",
]
