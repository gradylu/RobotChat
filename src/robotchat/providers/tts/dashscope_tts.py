"""云端 TTS 驱动（DashScope CosyVoice 流式，T-2.5）。

输出统一为 PCM16LE / 单声道，采样率与音色可配，可直接替换本地 Qwen3-TTS。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from robotchat.audio.frame import AudioChunk
from robotchat.core.cancel import CancellationToken
from robotchat.health import HealthStatus
from robotchat.observability.logging import get_logger
from robotchat.providers._ws import ConnectFactory, default_connect
from robotchat.providers.base import TTSCapabilities, TTSRequest
from robotchat.providers.registry import register_provider
from robotchat.providers.tts.qwen3_tts import TTSUnavailableError

logger = get_logger("robotchat.providers.tts.dashscope_tts")

DEFAULT_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
DEFAULT_VOICES = ("default", "zh_female", "yue_female", "en_female")


@register_provider("tts", "dashscope_tts")
class DashScopeTTS:
    name = "dashscope_tts"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        connect: ConnectFactory | None = None,
        client: httpx.AsyncClient | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        cfg = config or {}
        self.model = str(cfg.get("model", "cosyvoice-v1") or "cosyvoice-v1")
        self.ws_url = str(cfg.get("ws_url", DEFAULT_WS_URL) or DEFAULT_WS_URL)
        self.probe_url = str(cfg.get("probe_url", "https://dashscope.aliyuncs.com/api/v1") or "")
        self.api_key_env = str(cfg.get("api_key_env", "DASHSCOPE_API_KEY") or "")
        self.voice = str(cfg.get("voice", "default") or "default")
        self.sample_rate = int(cfg.get("sample_rate", 16000) or 16000)
        self.format = str(cfg.get("format", "pcm") or "pcm")
        voices = tuple(str(item) for item in cfg.get("voices", DEFAULT_VOICES))
        languages = frozenset(
            str(item) for item in cfg.get("languages", ("zh", "yue", "en")) if str(item)
        )
        self.capabilities = TTSCapabilities(
            streaming=True,
            sample_rate=self.sample_rate,
            voices=voices,
            languages=languages or frozenset({"zh"}),
        )
        self.timeout_s = float(cfg.get("timeout_ms", 30000) or 30000) / 1000.0
        self._connect = connect or default_connect
        self._client = client
        self._owns_client = client is None
        self._environ = environ

    def _api_key(self) -> str:
        env = os.environ if self._environ is None else self._environ
        return env.get(self.api_key_env, "") if self.api_key_env else ""

    async def synthesize_stream(
        self, req: TTSRequest, cancel: CancellationToken
    ) -> AsyncIterator[AudioChunk]:
        api_key = self._api_key()
        if not api_key:
            raise TTSUnavailableError(f"缺少云端 TTS 凭据：环境变量 {self.api_key_env} 未设置")
        voice = req.voice if req.voice and req.voice != "default" else self.voice
        sample_rate = req.sample_rate or self.sample_rate
        ws = await self._connect(self.ws_url, {"Authorization": f"bearer {api_key}"})
        task_id = uuid.uuid4().hex
        run_task = {
            "header": {
                "action": "run-task",
                "task_id": task_id,
                "streaming": "duplex",
                "task_group": "audio",
                "task": "tts",
                "function": "SpeechSynthesizer",
                "model": self.model,
            },
            "payload": {
                "task_group": "audio",
                "task": "tts",
                "function": "SpeechSynthesizer",
                "model": self.model,
                "parameters": {
                    "text": req.text,
                    "voice": voice,
                    "format": self.format,
                    "sample_rate": sample_rate,
                },
                "input": {"text": req.text},
            },
        }
        finish_task = {
            "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"input": {}},
        }
        try:
            await ws.send(json.dumps(run_task, ensure_ascii=False))
            await ws.send(json.dumps(finish_task, ensure_ascii=False))
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.timeout_s)
                except asyncio.TimeoutError as exc:
                    raise TTSUnavailableError("云端 TTS 响应超时") from exc
                if cancel.cancelled:
                    return
                if isinstance(raw, (bytes, bytearray)):
                    yield AudioChunk(
                        data=bytes(raw), sample_rate=sample_rate, channels=1, bit_depth=16
                    )
                    continue
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                header = message.get("header") or {}
                event = str(header.get("event", "") or "")
                if event == "result-generated":
                    audio = _extract_audio(message)
                    if audio:
                        yield AudioChunk(
                            data=audio, sample_rate=sample_rate, channels=1, bit_depth=16
                        )
                elif event == "task-finished":
                    break
                elif event == "task-failed":
                    detail = str(header.get("error_message", "") or "云端 TTS 任务失败")
                    raise TTSUnavailableError(detail)
        finally:
            with contextlib.suppress(Exception):  # 关闭失败不影响上层
                await ws.close()
        logger.info("tts_stream_finished", driver=self.name, voice=voice, chars=len(req.text))

    async def health(self) -> HealthStatus:
        if not self._api_key():
            return HealthStatus.unavailable(f"缺少云端 TTS 凭据：{self.api_key_env}")
        client = self._client or httpx.AsyncClient()
        started = time.monotonic()
        try:
            response = await client.get(self.probe_url, timeout=1.0)
        except Exception as exc:
            return HealthStatus.unavailable(repr(exc))
        finally:
            if self._client is None:
                await client.aclose()
        latency = round((time.monotonic() - started) * 1000.0, 2)
        return HealthStatus.ok(f"HTTP {response.status_code}", latency_ms=latency)

    async def warmup(self) -> None:
        status = await self.health()
        if status.state == "unavailable":
            raise RuntimeError(status.detail or "DashScope TTS 不可达")

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None


def _extract_audio(message: dict[str, Any]) -> bytes:
    payload = message.get("payload") or {}
    output = payload.get("output") or {}
    candidates = [
        output.get("audio"),
        output.get("audio_data"),
        payload.get("audio"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get("data")
        if not isinstance(candidate, str) or not candidate:
            continue
        try:
            return base64.b64decode(candidate)
        except (binascii.Error, ValueError):
            continue
    return b""


__all__ = ["DashScopeTTS"]
