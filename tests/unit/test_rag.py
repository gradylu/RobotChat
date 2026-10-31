"""知识检索驱动与调度单测：命中、200ms 超时跳过、5xx 跳过、none 关闭（T-2.12）。"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from robotchat.dialog.rag import CONTEXT_HEADER, RagService
from robotchat.providers.base import KnowledgeHit
from robotchat.providers.knowledge.http_rag import (
    HTTPRagRetriever,
    NoopRetriever,
    RagUnavailableError,
)
from robotchat.providers.registry import ProviderRegistry


class NeverCancelled:
    @property
    def cancelled(self) -> bool:
        return False

    async def wait(self) -> None:
        return None


class _FastRetriever:
    async def retrieve(
        self, query: str, top_k: int, cancel: Any
    ) -> list[KnowledgeHit]:
        return [
            KnowledgeHit(doc_id="d1", text="充电桩位于 A 区", score=0.9),
            KnowledgeHit(doc_id="d2", text="充电需刷卡", score=0.7),
        ]


class _SlowRetriever:
    async def retrieve(
        self, query: str, top_k: int, cancel: Any
    ) -> list[KnowledgeHit]:
        await asyncio.sleep(0.2)
        return [KnowledgeHit(doc_id="d1", text="迟到的结果")]


class _BrokenRetriever:
    async def retrieve(
        self, query: str, top_k: int, cancel: Any
    ) -> list[KnowledgeHit]:
        raise RuntimeError("HTTP 500")


async def test_rag_returns_hits_and_builds_context() -> None:
    service = RagService(_FastRetriever(), timeout_ms=200, max_chars=1200)
    outcome = await service.retrieve("充电桩在哪", NeverCancelled())

    assert [hit.doc_id for hit in outcome.hits] == ["d1", "d2"]
    assert outcome.skipped is False
    assert service.stats.hits == 2
    context = service.build_context(outcome)
    assert context.startswith(CONTEXT_HEADER)
    assert "充电桩位于 A 区" in context


async def test_rag_timeout_skips_without_blocking() -> None:
    service = RagService(_SlowRetriever(), timeout_ms=20)
    outcome = await service.retrieve("充电桩在哪", NeverCancelled())

    assert outcome.hits == ()
    assert outcome.skipped is True
    assert "timeout" in outcome.reason
    assert service.stats.skipped == 1
    assert service.stats.as_dict()["rag_skipped"] == 1


async def test_rag_failure_skips_and_counts() -> None:
    service = RagService(_BrokenRetriever(), timeout_ms=200)
    outcome = await service.retrieve("充电桩在哪", NeverCancelled())

    assert outcome.skipped is True
    assert "HTTP 500" in outcome.reason
    assert service.stats.skipped == 1
    assert service.build_context(outcome) == ""


async def test_rag_disabled_by_none_driver() -> None:
    service = RagService(NoopRetriever())
    outcome = await service.retrieve("充电桩在哪", NeverCancelled())
    assert outcome.hits == ()
    assert outcome.skipped is False
    assert service.stats.requests == 1


async def test_rag_without_retriever_is_disabled() -> None:
    service = RagService(None)
    outcome = await service.retrieve("充电桩在哪", NeverCancelled())
    assert outcome.reason == "disabled"
    assert service.stats.requests == 0


async def test_http_rag_parses_hits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "hits": [
                    {"doc_id": "kb-1", "text": "会议室在 3 楼", "score": 0.88},
                    {"id": "kb-2", "content": "访客需登记"},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    retriever = HTTPRagRetriever({"base_url": "http://10.0.0.12:9000"}, client=client)
    hits = await retriever.retrieve("会议室", 4, NeverCancelled())
    await client.aclose()

    assert [hit.doc_id for hit in hits] == ["kb-1", "kb-2"]
    assert hits[0].text == "会议室在 3 楼"


async def test_http_rag_5xx_raises_for_service_to_skip() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    )
    retriever = HTTPRagRetriever({"base_url": "http://10.0.0.12:9000"}, client=client)
    with pytest.raises(RagUnavailableError, match="503"):
        await retriever.retrieve("会议室", 4, NeverCancelled())
    await client.aclose()


def test_knowledge_slot_registered_only_when_configured() -> None:
    registry = ProviderRegistry()
    registry.register_slot("knowledge", "http_rag", {"base_url": "http://10.0.0.12:9000"})
    slot = registry.slot("knowledge")
    assert slot is not None
    assert slot.primary.usable
