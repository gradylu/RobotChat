"""云端流式 ASR 驱动（DashScope 实时语音识别，T-2.2）。

与本地驱动输出同一套 `ASREvent` 语义（partial / final），因此编排层可在两者间互换；
热词通过 `vocabulary_id` 或 `hot_words` 透传。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import httpx

from robotchat.core.cancel import CancellationToken
from robotchat.health import HealthStatus
from robotchat.observability.logging import get_logger
from robotchat.providers.asr._ws import ConnectFactory, WebSocketASRStream, default_connect
from robotchat.providers.base import ASRCapabilities, ASREvent, ASRStream
from robotchat.providers.registry import register_provider

logger = get_logger("robotchat.providers.asr.dashscope_stream")

DEFAULT_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"


class _DashScopeMapper:
    """把 DashScope 的 result-generated / task-finished 映射为 partial / final。"""

    def __init__(self) -> None:
        self._last_text = ""

    def __call__(self, message: dict[str, Any]) -> ASREvent | None:
        header = message.get("header") or {}
        event_name = str(header.get("event", "") or "")
        if event_name == "result-generated":
            payload = message.get("payload") or {}
            output = payload.get("output") or {}
            sentence = output.get("sentence") or {}
            text = str(sentence.get("text", "") or "").strip()
            if not text:
                return None
            self._last_text = text
            lang = str(sentence.get("language", "") or "")
            if bool(sentence.get("sentence_end")):
                return ASREvent(type="final", text=text, lang=lang, ts=time.time())
            return ASREvent(type="partial", text=text, lang=lang, ts=time.time())
        if event_name == "task-failed":
            error = str(
                header.get("error_message", "") or message.get("payload", "")
            )
            return ASREvent(
                type="error", error=error or "DashScope ASR task failed", ts=time.time()
            )
        if event_name == "task-finished":
            text = self._last_text
            self._last_text = ""
            return ASREvent(type="final", text=text, ts=time.time())
        return None


@register_provider("asr", "dashscope_stream")
class DashScopeASR:
    name = "dashscope_stream"
    capabilities = ASRCapabilities(
        streaming=True,
        hotwords=True,
        timestamps=True,
        languages=frozenset({"zh"}),
        sample_rates=(16000,),
    )

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        connect: ConnectFactory | None = None,
        client: httpx.AsyncClient | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        cfg = config or {}
        self.model = str(cfg.get("model", "paraformer-realtime-v2") or "paraformer-realtime-v2")
        self.ws_url = str(cfg.get("ws_url", DEFAULT_WS_URL) or DEFAULT_WS_URL)
        self.probe_url = str(
            cfg.get("probe_url", "https://dashscope.aliyuncs.com/api/v1") or ""
        )
        self.api_key_env = str(cfg.get("api_key_env", "DASHSCOPE_API_KEY") or "")
        self.sample_rate = int(cfg.get("sample_rate", 16000) or 16000)
        self.languages = frozenset(
            str(item) for item in cfg.get("languages", ("zh",)) if str(item)
        )
        self.language_hints = [str(item) for item in cfg.get("language_hints", ("zh",))]
        self.capabilities = ASRCapabilities(
            streaming=True,
            hotwords=True,
            timestamps=True,
            languages=self.languages or frozenset({"zh"}),
            sample_rates=(self.sample_rate,),
        )
        self.vocabulary_id = str(cfg.get("vocabulary_id", "") or "")
        self.hot_words = _hotword_list(cfg.get("hotwords", ()))
        self.final_timeout_s = float(cfg.get("final_timeout_ms", 1500) or 1500) / 1000.0
        self._connect = connect or default_connect
        self._client = client
        self._environ = environ

    def _api_key(self) -> str:
        env = os.environ if self._environ is None else self._environ
        return env.get(self.api_key_env, "") if self.api_key_env else ""

    async def open_stream(
        self, session_id: str, cancel: CancellationToken, lang: str = ""
    ) -> ASRStream:
        del cancel  # 取消由流内消费循环负责
        api_key = self._api_key()
        if not api_key:
            raise RuntimeError(f"缺少云端 ASR 凭据：环境变量 {self.api_key_env} 未设置")
        headers = {"Authorization": f"bearer {api_key}"}
        ws = await self._connect(self.ws_url, headers)
        task_id = uuid.uuid4().hex
        hints = [lang] if lang and lang in self.languages else self.language_hints
        parameters: dict[str, Any] = {
            "format": "pcm",
            "sample_rate": self.sample_rate,
            "language_hints": hints,
            "disfluency_removal_enabled": False,
        }
        if self.vocabulary_id:
            parameters["vocabulary_id"] = self.vocabulary_id
        if self.hot_words:
            parameters["hot_words"] = self.hot_words
        run_task = {
            "header": {
                "action": "run-task",
                "task_id": task_id,
                "streaming": "duplex",
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self.model,
            },
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self.model,
                "parameters": parameters,
                "input": {},
            },
        }
        await ws.send(json.dumps(run_task, ensure_ascii=False))
        finish_task = {
            "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"input": {}},
        }
        logger.info("asr_stream_opened", driver=self.name, session_id=session_id, model=self.model)
        return WebSocketASRStream(
            ws,
            _DashScopeMapper(),
            finish_task,
            final_timeout_s=self.final_timeout_s,
        )

    async def health(self) -> HealthStatus:
        if not self._api_key():
            return HealthStatus.unavailable(f"缺少云端 ASR 凭据：{self.api_key_env}")
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
            raise RuntimeError(status.detail or "DashScope ASR 不可达")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _hotword_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


__all__ = ["DashScopeASR"]
