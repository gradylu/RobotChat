"""外置 FunASR 方言模型 ASR 驱动（T-2A.3、FR-15.8）。

方言模型体积大且更新独立，不进应用镜像（CON-8）；每种语言对应一个外置 FunASR 实例，
或由同一实例的合并模型承载。服务地址、模型名与热词均由配置声明。
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
from robotchat.providers.asr.funasr_local import _hotword_text, _map_message, _ws_url
from robotchat.providers.base import ASRCapabilities, ASRStream
from robotchat.providers.registry import register_provider

logger = get_logger("robotchat.providers.asr.funasr_dialect")


@register_provider("asr", "funasr_dialect")
class FunASRDialectASR:
    name = "funasr_dialect"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        connect: ConnectFactory | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        cfg = config or {}
        endpoints = {
            _key(name): str(url).strip()
            for name, url in (cfg.get("endpoints") or {}).items()
            if str(url).strip()
        }
        default = str(cfg.get("endpoint", "") or "").strip()
        if not endpoints and not default:
            raise ValueError("funasr_dialect 缺少 endpoints 或 endpoint（外置方言模型地址）")
        self.endpoints = {lang: _ws_url(url) for lang, url in endpoints.items()}
        self.default_endpoint = _ws_url(default) if default else ""
        declared = cfg.get("languages") or list(self.endpoints) or []
        self.languages = frozenset(_key(item) for item in declared if str(item))
        self.sample_rate = int(cfg.get("sample_rate", 16000) or 16000)
        self.mode = str(cfg.get("mode", "2pass") or "2pass")
        self.hotwords = _hotword_text(cfg.get("hotwords", ()))
        self.chunk_size = list(cfg.get("chunk_size", (5, 10, 5)))
        self.models = {
            _key(name): str(model)
            for name, model in (cfg.get("models") or {}).items()
            if str(model).strip()
        }
        self.final_timeout_s = float(cfg.get("final_timeout_ms", 1200) or 1200) / 1000.0
        self.capabilities = ASRCapabilities(
            streaming=True,
            hotwords=True,
            timestamps=True,
            languages=self.languages or frozenset({"zh"}),
            sample_rates=(self.sample_rate,),
        )
        self._connect = connect or default_connect
        self._client = client

    def endpoint_for(self, lang: str) -> str:
        target = _key(lang)
        endpoint = self.endpoints.get(target) or self.default_endpoint
        if not endpoint:
            raise RuntimeError(f"方言 ASR 未配置语言 {lang or '?'} 的服务地址（FR-15.8）")
        return endpoint

    async def open_stream(
        self, session_id: str, cancel: CancellationToken, lang: str = ""
    ) -> ASRStream:
        del cancel  # 取消由流内消费循环负责
        endpoint = self.endpoint_for(lang)
        ws = await self._connect(endpoint, {})
        start: dict[str, Any] = {
            "mode": self.mode,
            "wav_name": session_id,
            "wav_format": "pcm",
            "audio_fs": self.sample_rate,
            "chunk_size": self.chunk_size,
            "is_speaking": True,
            "itn": True,
            "lang": _key(lang),
        }
        model = self.models.get(_key(lang))
        if model:
            start["model"] = model
        if self.hotwords:
            start["hotwords"] = self.hotwords
        await ws.send(json.dumps(start, ensure_ascii=False))
        logger.info(
            "asr_stream_opened",
            driver=self.name,
            session_id=session_id,
            lang=_key(lang),
            endpoint=endpoint,
        )
        return WebSocketASRStream(
            ws,
            _map_message,
            {"is_speaking": False},
            final_timeout_s=self.final_timeout_s,
        )

    async def health(self, lang: str = "") -> HealthStatus:
        endpoint = self.endpoints.get(_key(lang)) or self.default_endpoint
        if not endpoint:
            return HealthStatus.unavailable("方言 ASR 未配置服务地址")
        probe = _http_url(endpoint)
        client = self._client or httpx.AsyncClient()
        started = time.monotonic()
        try:
            response = await client.get(probe, timeout=0.5)
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
            raise RuntimeError(status.detail or "方言 ASR 服务不可达")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _key(value: str) -> str:
    return (value or "").strip().lower().replace("_", "-").split("-")[0]


def _http_url(ws_url: str) -> str:
    if ws_url.startswith("wss://"):
        return "https://" + ws_url[len("wss://") :]
    if ws_url.startswith("ws://"):
        return "http://" + ws_url[len("ws://") :]
    return ws_url


__all__ = ["FunASRDialectASR"]
