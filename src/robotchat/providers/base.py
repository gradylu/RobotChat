"""Provider 抽象接口与能力声明（FR-10，design §7.1）。

除唤醒词 KWS 与 VAD 外，provider 一律通过 HTTP / WebSocket 访问外部模型服务，
不做进程内推理（CON-1、ADR-04、ADR-12）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from robotchat.audio.frame import AudioChunk, AudioFrame
from robotchat.core.cancel import CancellationToken
from robotchat.health import HealthStatus
from robotchat.tools.base import ToolDefinition

# ---------------------------------------------------------------- 能力声明


@dataclass(frozen=True, slots=True)
class ASRCapabilities:
    streaming: bool = True
    hotwords: bool = False
    timestamps: bool = False
    languages: frozenset[str] = frozenset({"zh"})
    sample_rates: tuple[int, ...] = (16000,)


@dataclass(frozen=True, slots=True)
class LLMCapabilities:
    streaming: bool = True
    tool_call: bool = True
    context_window: int = 8192


@dataclass(frozen=True, slots=True)
class TTSCapabilities:
    streaming: bool = True
    sample_rate: int = 16000
    voices: tuple[str, ...] = ("default",)
    languages: frozenset[str] = frozenset({"zh"})


@dataclass(frozen=True, slots=True)
class KwsCapabilities:
    multi_wakeword: bool = True
    custom_phrase: bool = False
    sample_rate: int = 16000
    frame_ms: int = 80


@dataclass(frozen=True, slots=True)
class VadCapabilities:
    sample_rate: int = 16000
    frame_ms: int = 20


# ---------------------------------------------------------------- 事件与请求


@dataclass(frozen=True, slots=True)
class ASREvent:
    type: Literal["partial", "final", "error"]
    text: str = ""
    lang: str = ""
    confidence: float = 0.0
    ts: float = 0.0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str


@dataclass(frozen=True, slots=True)
class ChatRequest:
    messages: list[ChatMessage]
    tools: list[ToolDefinition] = field(default_factory=list)
    lang: str = "zh"
    session_id: str = ""
    turn_id: int = 0


@dataclass(frozen=True, slots=True)
class LLMEvent:
    type: Literal["token", "tool_call", "done", "error"]
    text: str = ""
    tool_call: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class TTSRequest:
    text: str
    voice: str = "default"
    sample_rate: int = 16000
    lang: str = "zh"
    turn_id: int = 0


@dataclass(frozen=True, slots=True)
class WakeEvent:
    type: Literal["detected", "error"]
    phrase: str = ""
    confidence: float = 0.0
    ts: float = 0.0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    doc_id: str
    text: str
    score: float = 0.0
    skipped: bool = False


@dataclass(frozen=True, slots=True)
class VadEvent:
    type: Literal["speech_start", "speech_end", "silence"]
    energy_db: float = 0.0
    ts: float = 0.0


# ---------------------------------------------------------------- Provider 契约


class ASRStream(Protocol):
    async def feed(self, frame: AudioFrame) -> None: ...

    def events(self) -> AsyncIterator[ASREvent]: ...

    async def close(self) -> None: ...


class ASRProvider(Protocol):
    name: str
    capabilities: ASRCapabilities

    async def open_stream(
        self, session_id: str, cancel: CancellationToken, lang: str = ""
    ) -> ASRStream: ...

    async def health(self) -> HealthStatus: ...

    async def warmup(self) -> None:
        """预热，规避模型冷启动（ADR-04）。"""


class LLMProvider(Protocol):
    name: str
    capabilities: LLMCapabilities

    async def chat_stream(
        self, req: ChatRequest, cancel: CancellationToken
    ) -> AsyncIterator[LLMEvent]: ...

    async def health(self) -> HealthStatus: ...


class TTSProvider(Protocol):
    name: str
    capabilities: TTSCapabilities

    async def synthesize_stream(
        self, req: TTSRequest, cancel: CancellationToken
    ) -> AsyncIterator[AudioChunk]: ...

    async def health(self) -> HealthStatus: ...


class KwsStream(Protocol):
    async def feed(self, frame: AudioFrame) -> None: ...

    async def close(self) -> None: ...


class WakeWordProvider(Protocol):
    name: str
    capabilities: KwsCapabilities

    async def open_stream(self, cancel: CancellationToken) -> KwsStream: ...

    def events(self) -> AsyncIterator[WakeEvent]: ...

    async def health(self) -> HealthStatus: ...

    async def warmup(self) -> None:
        """加载 ONNX 模型到内存，规避首次唤醒卡顿。"""


class VadEngine(Protocol):
    name: str
    capabilities: VadCapabilities

    def process(self, frame: AudioFrame) -> VadEvent | None: ...

    def reset(self) -> None: ...

    async def health(self) -> HealthStatus: ...


class KnowledgeRetriever(Protocol):
    name: str

    async def retrieve(
        self, query: str, top_k: int, cancel: CancellationToken
    ) -> list[KnowledgeHit]: ...

    async def health(self) -> HealthStatus: ...


class MemoryStore(Protocol):
    name: str

    async def append_message(self, session_id: str, message: ChatMessage) -> None: ...

    async def history(self, session_id: str, limit: int) -> list[ChatMessage]: ...

    async def clear(self, session_id: str) -> None: ...

    async def health(self) -> HealthStatus: ...
