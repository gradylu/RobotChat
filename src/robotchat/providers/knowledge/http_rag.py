"""HTTP 知识库检索驱动（边端 / 云端，A5、T-2.12）。

知识库不在本体：应用只做检索调用，超时或 5xx 由 `dialog/rag.py` 跳过并计数。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from robotchat.core.cancel import CancellationToken
from robotchat.health import HealthStatus
from robotchat.observability.logging import get_logger
from robotchat.providers.base import KnowledgeHit
from robotchat.providers.registry import register_provider

logger = get_logger("robotchat.providers.knowledge.http_rag")


class RagUnavailableError(RuntimeError):
    """知识库不可达或返回错误，上层应跳过增强而非阻塞对话。"""


@register_provider("knowledge", "http_rag")
class HTTPRagRetriever:
    name = "http_rag"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        cfg = config or {}
        base_url = str(cfg.get("base_url", "") or "").strip()
        if not base_url or "${" in base_url:
            raise ValueError("http_rag 缺少 base_url（KB_BASE_URL），知识库增强已关闭")
        self.base_url = base_url.rstrip("/")
        self.path = str(cfg.get("path", "/retrieve") or "/retrieve")
        self.timeout_ms = int(cfg.get("timeout_ms", 200) or 200)
        self.max_chars = int(cfg.get("max_chars", 1200) or 1200)
        self.api_key_env = str(cfg.get("api_key_env", "") or "")
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        import os

        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self.api_key_env, "") if self.api_key_env else ""
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    async def retrieve(
        self, query: str, top_k: int, cancel: CancellationToken
    ) -> list[KnowledgeHit]:
        if cancel.cancelled:
            return []
        timeout = httpx.Timeout(self.timeout_ms / 1000.0)
        try:
            response = await self._http().post(
                self.base_url + self.path,
                json={"query": query, "top_k": top_k, "max_chars": self.max_chars},
                headers=self._headers(),
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise RagUnavailableError(repr(exc)) from exc
        if response.status_code >= 400:
            raise RagUnavailableError(f"HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RagUnavailableError(f"知识库响应不是合法 JSON：{exc!r}") from exc
        items = data.get("hits") or data.get("documents") or data.get("results") or []
        hits: list[KnowledgeHit] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("content") or "")
            if not text:
                continue
            hits.append(
                KnowledgeHit(
                    doc_id=str(item.get("doc_id") or item.get("id") or ""),
                    text=text,
                    score=float(item.get("score") or 0.0),
                )
            )
        return hits[:top_k]

    async def health(self) -> HealthStatus:
        started = time.monotonic()
        try:
            response = await self._http().get(self.base_url, timeout=0.5)
        except Exception as exc:
            return HealthStatus.unavailable(repr(exc))
        latency = round((time.monotonic() - started) * 1000.0, 2)
        return HealthStatus.ok(f"HTTP {response.status_code}", latency_ms=latency)

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None


@register_provider("knowledge", "none")
class NoopRetriever:
    """关闭知识库增强（`providers.knowledge.active=none`）。"""

    name = "none"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        del config

    async def retrieve(
        self, query: str, top_k: int, cancel: CancellationToken
    ) -> list[KnowledgeHit]:
        del query, top_k, cancel
        return []

    async def health(self) -> HealthStatus:
        return HealthStatus.ok("知识库增强已关闭")


__all__ = ["HTTPRagRetriever", "NoopRetriever", "RagUnavailableError"]
