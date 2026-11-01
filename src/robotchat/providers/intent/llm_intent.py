"""llm_intent：基于 OpenAI 兼容 chat/completions 的语义分类驱动（FR-17，design §7.3）。

- 只收文本，不传音频（FR-17.10）；
- 意图枚举 / 描述 / 示例全部由 config/intent.yaml 注入（C-178）；
- 走 registry 槽位熔断（§7.5）；路由层的 `classify_timeout_ms` 是最终预算，
  驱动内部 HTTP 超时略大于该值，保证先由路由层判超时并兜底 chat。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

import httpx

from robotchat.health import HealthStatus
from robotchat.observability.logging import get_logger
from robotchat.providers.base import (
    IntentCapabilities,
    IntentRequest,
    IntentResult,
)
from robotchat.providers.registry import register_provider

logger = get_logger("robotchat.providers.intent.llm_intent")

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")

DEFAULT_TIMEOUT_MS = 500


def _extract_json(text: str) -> dict[str, Any]:
    """解析模型输出：容忍 ```json 围栏与前后杂散文本，取第一个 JSON 对象。"""
    cleaned = _FENCE_RE.sub("", (text or "").strip()).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("意图分类输出必须是 JSON 对象")
    return data


def _build_system_prompt(intents: dict[str, dict[str, Any]]) -> str:
    lines = [
        "你是机器人语音助手的意图分类器。根据用户话语从以下意图中选择唯一一个，",
        '只输出 JSON：{"intent": "<意图名>", "confidence": <0~1>, "slots": {}}。',
        "confidence 为该判定的置信度；无法归类时选 chat。",
        "可用意图：",
    ]
    for name, item in intents.items():
        description = str(item.get("description") or "")
        examples = [str(example) for example in (item.get("examples") or [])[:5]]
        suffix = f"，示例：{' / '.join(examples)}" if examples else ""
        lines.append(f"- {name}: {description}{suffix}")
    lines.append(
        '仅当意图为 command 时在 slots 中给出 {"tool": "<工具名>", "arguments": {参数}}；'
        "其余意图 slots 留空对象。"
    )
    return "\n".join(lines)


@register_provider("intent", "llm_intent")
class LlmIntentProvider:
    """调用 OpenAI 兼容接口做小样本意图分类；温度 0、max_tokens 128。"""

    def __init__(self, config: dict[str, Any]) -> None:
        cfg = dict(config or {})
        self.name = "llm_intent"
        self._base_url = str(cfg.get("base_url", "") or "").strip().rstrip("/")
        self._model = str(cfg.get("model", "") or "")
        self._api_key_env = str(cfg.get("api_key_env", "") or "")
        self._timeout_ms = int(cfg.get("timeout_ms", DEFAULT_TIMEOUT_MS) or DEFAULT_TIMEOUT_MS)
        intents_cfg = cfg.get("intents") or {}
        self._intents: dict[str, dict[str, Any]] = (
            {str(k): dict(v) if isinstance(v, dict) else {} for k, v in intents_cfg.items()}
            if isinstance(intents_cfg, dict)
            else {}
        )
        names = frozenset(self._intents) if self._intents else frozenset({"chat"})
        supports_slots = bool(cfg.get("supports_slots", True))
        self.capabilities = IntentCapabilities(
            intents=names, supports_slots=supports_slots, hot_swap=True
        )
        self._system_prompt = _build_system_prompt(self._intents)
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------ 内部

    def _unconfigured_reason(self) -> str:
        if not self._base_url or "${" in self._base_url:
            return "base_url 未配置（设置 LLM_BASE_URL）"
        if not self._model or "${" in self._model:
            return "model 未配置（设置 LLM_MODEL）"
        return ""

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key_env:
            key = os.environ.get(self._api_key_env, "")
            if key:
                headers["Authorization"] = f"Bearer {key}"
        return headers

    def _client_with_timeout(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout_ms / 1000.0 + 0.5, follow_redirects=False
            )
        return self._client

    # ------------------------------------------------------------ 契约

    async def classify(
        self, req: IntentRequest, cancel: Any = None
    ) -> IntentResult:
        """一次语义分类；异常向上抛出，由路由层统一兜底 chat。"""
        unconfigured = self._unconfigured_reason()
        if unconfigured:
            raise RuntimeError(f"llm_intent 未配置：{unconfigured}")
        started = time.monotonic()
        text = (req.text or "").strip()
        if not text:
            raise ValueError("意图分类输入为空")
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": text},
            ],
            "temperature": 0,
            "max_tokens": 128,
            "stream": False,
        }
        http = self._client_with_timeout()
        response = await http.post(
            f"{self._base_url}/chat/completions", json=payload, headers=self._headers()
        )
        response.raise_for_status()
        body = response.json()
        content = str(
            (((body or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        )
        data = _extract_json(content)
        intent = str(data.get("intent") or "").strip()
        if not intent:
            raise ValueError("意图分类输出缺少 intent")
        confidence = float(data.get("confidence") or 0.0)
        slots_raw = data.get("slots")
        slots = dict(slots_raw) if isinstance(slots_raw, dict) else {}
        latency = (time.monotonic() - started) * 1000.0
        return IntentResult(
            intent=intent,
            confidence=max(0.0, min(1.0, confidence)),
            slots=slots,
            source="llm",
            latency_ms=round(latency, 2),
        )

    async def health(self) -> HealthStatus:
        unconfigured = self._unconfigured_reason()
        if unconfigured:
            return HealthStatus.unavailable(unconfigured)
        http = self._client_with_timeout()
        try:
            response = await asyncio.wait_for(
                http.get(f"{self._base_url}/models", headers=self._headers()),
                timeout=self._timeout_ms / 1000.0 + 0.5,
            )
            response.raise_for_status()
        except Exception as exc:
            return HealthStatus.unavailable(repr(exc))
        return HealthStatus.ok(f"{self._model} 可达")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = ["LlmIntentProvider"]
