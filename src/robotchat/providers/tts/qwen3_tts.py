"""本地 Qwen3-TTS 流式驱动（T-2.4）。

服务以独立进程 / 容器运行（ADR-04），本驱动通过 HTTP 分块流式读取 PCM16LE；
音色与采样率可配，输出格式与云端驱动一致（PCM16LE / 单声道），两者可互换。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from robotchat.audio.frame import AudioChunk
from robotchat.core.cancel import CancellationToken
from robotchat.health import HealthStatus
from robotchat.observability.logging import get_logger
from robotchat.providers.base import TTSCapabilities, TTSRequest
from robotchat.providers.registry import register_provider

logger = get_logger("robotchat.providers.tts.qwen3_tts")

DEFAULT_VOICES = ("default", "zh_female", "en_female", "en_male")


class TTSUnavailableError(RuntimeError):
    """TTS 服务不可用或返回错误。"""


@register_provider("tts", "qwen3_tts_local")
class Qwen3TTSLocal:
    name = "qwen3_tts_local"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        cfg = config or {}
        base_url = str(cfg.get("base_url", "") or "").strip()
        if not base_url or "${" in base_url:
            raise ValueError("qwen3_tts_local 缺少 base_url（本地 TTS 服务地址）")
        self.base_url = base_url.rstrip("/")
        self.path = str(cfg.get("path", "/audio/speech") or "/audio/speech")
        self.voice = str(cfg.get("voice", "default") or "default")
        self.sample_rate = int(cfg.get("sample_rate", 16000) or 16000)
        voices = tuple(str(item) for item in cfg.get("voices", DEFAULT_VOICES))
        languages = frozenset(
            str(item) for item in cfg.get("languages", ("zh", "en")) if str(item)
        )
        self.capabilities = TTSCapabilities(
            streaming=True,
            sample_rate=self.sample_rate,
            voices=voices,
            languages=languages or frozenset({"zh"}),
        )
        self.timeout_ms = int(cfg.get("timeout_ms", 30000) or 30000)
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        return self._client

    async def synthesize_stream(
        self, req: TTSRequest, cancel: CancellationToken
    ) -> AsyncIterator[AudioChunk]:
        voice = req.voice if req.voice and req.voice != "default" else self.voice
        sample_rate = req.sample_rate or self.sample_rate
        payload = {
            "text": req.text,
            "voice": voice,
            "sample_rate": sample_rate,
            "format": "pcm",
            "lang": req.lang,
        }
        timeout = httpx.Timeout(self.timeout_ms / 1000.0, connect=0.5)
        try:
            async with self._http().stream(
                "POST", self.base_url + self.path, json=payload, timeout=timeout
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:200]
                    raise TTSUnavailableError(f"HTTP {response.status_code}: {body}")
                async for chunk in response.aiter_bytes():
                    if cancel.cancelled:
                        return
                    if chunk:
                        yield AudioChunk(
                            data=chunk, sample_rate=sample_rate, channels=1, bit_depth=16
                        )
        except httpx.HTTPError as exc:
            raise TTSUnavailableError(repr(exc)) from exc

    async def health(self) -> HealthStatus:
        started = time.monotonic()
        try:
            response = await self._http().get(self.base_url, timeout=0.5)
        except Exception as exc:
            return HealthStatus.unavailable(repr(exc))
        latency = round((time.monotonic() - started) * 1000.0, 2)
        return HealthStatus.ok(f"HTTP {response.status_code}", latency_ms=latency)

    async def warmup(self) -> None:
        status = await self.health()
        if status.state == "unavailable":
            raise RuntimeError(status.detail or "Qwen3-TTS 服务不可达")

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None


__all__ = ["Qwen3TTSLocal", "TTSUnavailableError"]
