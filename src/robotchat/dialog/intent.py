"""意图识别与路由（R14 / FR-17，design §9.6）。

双路径设计：
- 规则快速路径：进程内匹配 yaml 中 `fast_path: true` 的意图（默认 stop / confirm /
  cancel），预算 P95 ≤ 20ms，串行前置计入首响；
- 语义分类（chat / knowledge / command）：可插拔 IntentProvider（registry 槽位，
  §7.5 熔断），与 LLM / RAG 并行，预算 `classify_timeout_ms`，超时 / 失败 /
  低置信度一律兜底 chat + WARN + intent_fallback_count。

约束：
- 意图集合全部来自 config/intent.yaml，本模块不硬编码意图清单（C-178）；
- confirm / cancel 仅在 FR-9.10 确认窗口内生效，窗口外按 chat（INV-11）；
- 意图不阻塞首响（INV-10）。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from typing import Any

from robotchat.config.schema import IntentSettings, Settings
from robotchat.core.cancel import CancellationToken
from robotchat.observability.logging import get_logger
from robotchat.observability.metrics import (
    INTENT_CLASSIFY,
    INTENT_FALLBACK,
    INTENT_LOW_CONFIDENCE,
    INTENT_RULE,
    INTENT_TOTAL,
    MetricsCollector,
)
from robotchat.providers.base import IntentRequest, IntentResult
from robotchat.providers.registry import ProviderRegistry

logger = get_logger("robotchat.dialog.intent")

# 语义锚点意图名：与 tools/policy.py 的确认判定词共用同一套语义角色。
# 意图「集合」来自 yaml（C-178）；以下是结构化语义（窗口 / 打断 / 兜底）引用的名称。
INTENT_CHAT = "chat"
INTENT_CONFIRM = "confirm"
INTENT_CANCEL = "cancel"
INTENT_STOP = "stop"
INTENT_COMMAND = "command"

# 去除空白与标点（保留中英数字），用于规则匹配的文本规整
_STRIP_RE = re.compile(r"[\s\W_]+", re.UNICODE)

# 单字示例只做精确匹配，避免「不停地说」这类包含误判
_SUBSTRING_MIN_CHARS = 2


def normalize_text(text: str) -> str:
    return _STRIP_RE.sub("", (text or "").casefold())


class IntentCommandRouted(Exception):
    """意图 command 命中且在首块音频前生效：中止 LLM 对话流，转工具执行（FR-17）。"""

    def __init__(self, result: IntentResult) -> None:
        super().__init__(f"intent command routed: {result.intent}")
        self.result = result


class IntentRuleMatcher:
    """规则快速路径：按配置遍历 fast_path 意图，规整文本后精确 / 包含 / 正则匹配。"""

    def __init__(self, settings: IntentSettings) -> None:
        self._entries: list[tuple[str, tuple[re.Pattern[str], ...], frozenset[str]]] = []
        for name, item in settings.intents.items():
            if not item.fast_path:
                continue
            patterns: list[re.Pattern[str]] = []
            for raw in (item.rules or {}).get("patterns") or []:
                try:
                    patterns.append(re.compile(str(raw)))
                except re.error:
                    logger.warning("intent_pattern_invalid", intent=name, pattern=str(raw))
            examples = frozenset(
                normalized
                for example in item.examples
                if (normalized := normalize_text(example))
            )
            if examples or patterns:
                self._entries.append((name, tuple(patterns), examples))

    def match(
        self, text: str, *, clock: Callable[[], float] = time.monotonic
    ) -> IntentResult | None:
        started = clock()
        normalized = normalize_text(text)
        if not normalized:
            return None
        for name, patterns, examples in self._entries:
            if normalized in examples:
                return self._hit(name, started, clock)
            for example in examples:
                if len(example) >= _SUBSTRING_MIN_CHARS and example in normalized:
                    return self._hit(name, started, clock)
            for pattern in patterns:
                if pattern.search(normalized):
                    return self._hit(name, started, clock)
        return None

    def _hit(
        self, name: str, started: float, clock: Callable[[], float]
    ) -> IntentResult:
        latency = (clock() - started) * 1000.0
        return IntentResult(
            intent=name, confidence=1.0, slots={}, source="rule", latency_ms=round(latency, 2)
        )


class IntentRouter:
    """双路径意图路由：规则快速路径 + 可插拔语义分类（FR-17）。"""

    def __init__(
        self,
        settings: Settings,
        registry: ProviderRegistry,
        metrics: MetricsCollector | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._metrics = metrics
        self._clock = clock
        self.apply_settings(settings)

    # ------------------------------------------------------------ 配置

    @property
    def enabled(self) -> bool:
        return self._intent.enabled

    @property
    def settings(self) -> IntentSettings:
        return self._intent

    def apply_settings(self, settings: Settings) -> None:
        """热更新入口：按最新 intent 配置重建匹配器（T-6.3）。"""
        self._intent = settings.intent
        self._matcher = IntentRuleMatcher(settings.intent)
        self._intent_names = frozenset(settings.intent.intents)

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self._intent.enabled,
            "active": self._intent.active,
            "confidence_threshold": self._intent.confidence_threshold,
            "classify_timeout_ms": self._intent.classify_timeout_ms,
            "gray_zone_top": self._intent.gray_zone_top,
            "intents": sorted(self._intent_names),
        }

    # ------------------------------------------------------------ 快速路径

    def fast_path(self, text: str, lang: str = "") -> IntentResult | None:
        """规则快速路径：命中返回 rule 结果（confidence=1.0），未命中返回 None。"""
        del lang  # 规则匹配与语言无关；保留参数以对齐分类入口
        result = self._matcher.match(text, clock=self._clock)
        if result is not None and self._metrics is not None:
            self._metrics.record(INTENT_RULE, max(0.0, result.latency_ms))
        return result

    # ------------------------------------------------------------ 语义分类

    async def classify(
        self,
        text: str,
        lang: str = "",
        cancel: CancellationToken | None = None,
        *,
        record_metrics: bool = True,
    ) -> IntentResult:
        """语义分类：超时 / 失败 / 低置信度 / 越界意图一律兜底 chat（FR-17）。

        `record_metrics=False` 供试跑入口使用，不污染运行指标。
        """
        started = self._clock()
        timeout_s = self._intent.classify_timeout_ms / 1000.0
        if record_metrics:
            self._note_total()

        def _fallback(reason: str) -> IntentResult:
            latency = (self._clock() - started) * 1000.0
            if record_metrics and self._metrics is not None:
                self._metrics.increment(INTENT_FALLBACK)
                self._metrics.record(INTENT_CLASSIFY, max(0.0, latency))
            logger.warning("intent_classify_fallback", reason=reason, latency_ms=round(latency, 2))
            return IntentResult(
                intent=INTENT_CHAT,
                confidence=0.0,
                slots={},
                source="llm",
                latency_ms=round(latency, 2),
            )

        async def _run() -> IntentResult:
            entry = await self._registry.acquire("intent")
            driver = entry.driver
            if driver is None:
                raise RuntimeError("意图分类驱动未初始化")
            request = IntentRequest(
                text=text, lang=lang, candidates=frozenset(self._intent_names)
            )
            return await driver.classify(request, cancel)

        try:
            raw = await asyncio.wait_for(_run(), timeout=timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _fallback(repr(exc))

        latency = (self._clock() - started) * 1000.0
        result = IntentResult(
            intent=str(raw.intent),
            confidence=max(0.0, min(1.0, float(raw.confidence))),
            slots=dict(raw.slots),
            source=raw.source if raw.source in ("rule", "model", "llm") else "llm",
            latency_ms=round(latency, 2),
        )
        if result.intent not in self._intent_names:
            # 结果意图必须 ∈ 配置集合，否则视为失败（C-178）
            return _fallback(f"未声明意图 {result.intent!r}")
        if result.confidence < self._intent.confidence_threshold:
            # 低置信度兜底 chat（INV-11：低置信度禁止触发工具）
            if record_metrics and self._metrics is not None:
                self._metrics.increment(INTENT_LOW_CONFIDENCE)
                self._metrics.record(INTENT_CLASSIFY, max(0.0, latency))
            logger.info(
                "intent_low_confidence",
                intent=result.intent,
                confidence=result.confidence,
                threshold=self._intent.confidence_threshold,
            )
            return IntentResult(
                intent=INTENT_CHAT,
                confidence=result.confidence,
                slots={},
                source=result.source,
                latency_ms=result.latency_ms,
            )
        if record_metrics and self._metrics is not None:
            self._metrics.record(INTENT_CLASSIFY, max(0.0, latency))
        return result

    # ------------------------------------------------------------ 路由判定

    def route(self, result: IntentResult, confirm_window_open: bool) -> str:
        """返回最终生效意图：窗口外 confirm / cancel 一律按 chat（FR-9.10 / INV-11）。"""
        if result.intent in (INTENT_CONFIRM, INTENT_CANCEL) and not confirm_window_open:
            return INTENT_CHAT
        return result.intent

    # ------------------------------------------------------------ 内部

    def _note_total(self) -> None:
        if self._metrics is not None:
            self._metrics.increment(INTENT_TOTAL)


__all__ = [
    "INTENT_CANCEL",
    "INTENT_CHAT",
    "INTENT_COMMAND",
    "INTENT_CONFIRM",
    "INTENT_STOP",
    "IntentCommandRouted",
    "IntentRouter",
    "IntentRuleMatcher",
    "normalize_text",
]
