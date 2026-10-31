"""本地 FunASR 流式 ASR 驱动（2pass：online partial + offline final，T-2.1）。

FunASR Runtime 作为独立服务运行（ADR-04），本驱动只做网络客户端：
- 连接 `ws://<endpoint>`，首帧发送 2pass 配置（含热词）；
- 逐帧写入 PCM16LE 音频；
- 服务端消息按 `mode` 区分 partial / final；
- 结束时发送 `is_speaking=false` 并等待最后一条 final。
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from robotchat.core.cancel import CancellationToken
from robotchat.health import HealthStatus
from robotchat.observability.logging import get_logger
from robotchat.providers.asr._ws import ConnectFactory, WebSocketASRStream, default_connect
from robotchat.providers.base import ASRCapabilities, ASREvent, ASRStream
from robotchat.providers.registry import register_provider

logger = get_logger("robotchat.providers.asr.funasr_local")


def _ws_url(endpoint: str) -> str:
    if endpoint.startswith("ws://") or endpoint.startswith("wss://"):
        return endpoint
    if endpoint.startswith("https://"):
        return "wss://" + endpoint[len("https://") :]
    if endpoint.startswith("http://"):
        return "ws://" + endpoint[len("http://") :]
    return "ws://" + endpoint


def _map_message(message: dict[str, Any]) -> ASREvent | None:
    text = str(message.get("text", "") or "").strip()
    if not text:
        return None
    mode = str(message.get("mode", "")).lower()
    is_final = (
        bool(message.get("is_final"))
        or "offline" in mode
        or str(message.get("type", "")) == "final"
    )
    return ASREvent(
        type="final" if is_final else "partial",
        text=text,
        lang=str(message.get("lang", "") or ""),
        confidence=float(message.get("confidence", 0.0) or 0.0),
        ts=float(message.get("ts", 0.0) or 0.0) or time.time(),
    )


@register_provider("asr", "funasr_local")
class FunASRLocalASR:
    name = "funasr_local"
    capabilities = ASRCapabilities(
        streaming=True,
        hotwords=True,
        timestamps=True,
        languages=frozenset({"zh", "en"}),
        sample_rates=(16000,),
    )

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        connect: ConnectFactory | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        cfg = config or {}
        endpoint = str(cfg.get("endpoint", "") or "").strip()
        if not endpoint:
            raise ValueError("funasr_local 缺少 endpoint（独立 FunASR 服务地址）")
        self.endpoint = endpoint.rstrip("/")
        self._ws = _ws_url(str(cfg.get("ws_url", "") or endpoint))
        self.sample_rate = int(cfg.get("sample_rate", 16000) or 16000)
        self.mode = str(cfg.get("mode", "2pass") or "2pass")
        self.hotwords = _hotword_text(cfg.get("hotwords", ()))
        self.chunk_size = list(cfg.get("chunk_size", (5, 10, 5)))
        self.final_timeout_s = float(cfg.get("final_timeout_ms", 1000) or 1000) / 1000.0
        self._connect = connect or default_connect
        self._client = client

    async def open_stream(
        self, session_id: str, cancel: CancellationToken, lang: str = ""
    ) -> ASRStream:
        del cancel  # 取消由流内消费循环负责
        ws = await self._connect(self._ws, {})
        start: dict[str, Any] = {
            "mode": self.mode,
            "wav_name": session_id,
            "wav_format": "pcm",
            "audio_fs": self.sample_rate,
            "chunk_size": self.chunk_size,
            "is_speaking": True,
            "itn": True,
        }
        if lang:
            # 本地 FunASR 以模型自带语言为准；声明 lang 便于服务端按需切换合并模型
            start["lang"] = lang
        if self.hotwords:
            start["hotwords"] = self.hotwords
        await ws.send(json.dumps(start, ensure_ascii=False))
        logger.info("asr_stream_opened", driver=self.name, session_id=session_id, mode=self.mode)
        return WebSocketASRStream(
            ws,
            _map_message,
            {"is_speaking": False},
            final_timeout_s=self.final_timeout_s,
        )

    async def health(self) -> HealthStatus:
        client = self._client or httpx.AsyncClient()
        started = time.monotonic()
        try:
            response = await client.get(self.endpoint, timeout=0.5)
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
            raise RuntimeError(status.detail or "FunASR 服务不可达")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _hotword_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return ""


__all__ = ["FunASRLocalASR"]
