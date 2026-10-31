"""语言路由、防抖与音色降级（T-2A.2 / T-2A.5 / T-2A.6 / T-2A.8、design §7.6）。

语言是数据不是分支：本模块只产出「本轮用什么语言、走哪个驱动、用哪个音色」的决策，
状态机与全双工链路保持与语言无关。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from robotchat.config.schema import Settings
from robotchat.observability.logging import get_logger

logger = get_logger("robotchat.dialog.language")

LANG_NAMES: dict[str, str] = {
    "zh": "普通话",
    "en": "英语",
    "yue": "粤语",
    "dongbei": "东北话",
    "wuhan": "武汉话",
    "shanghai": "上海话",
    "tianjin": "天津话",
}
DIALECTS = frozenset({"yue", "dongbei", "wuhan", "shanghai", "tianjin"})
RATIO_THRESHOLD = 0.7

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")
_ALIASES = {
    "zh-cn": "zh",
    "zh-hans": "zh",
    "zh-tw": "zh",
    "cmn": "zh",
    "en-us": "en",
    "en-gb": "en",
    "cantonese": "yue",
    "yue-hk": "yue",
}


def normalize_lang(value: str) -> str:
    """把 ASR / 配置给出的语言标签归一为内部短码（zh / en / yue ...）。"""
    raw = (value or "").strip().lower().replace("_", "-")
    if not raw:
        return ""
    if raw in _ALIASES:
        return _ALIASES[raw]
    return raw.split("-")[0]


def script_lang(text: str) -> str | None:
    """按字符占比判定主语言：任一中 / 英字符占比 ≥70% 才给出结论（FR-15.13）。"""
    cjk = len(_CJK.findall(text or ""))
    latin = len(_LATIN.findall(text or ""))
    total = cjk + latin
    if total == 0:
        return None
    if cjk / total >= RATIO_THRESHOLD:
        return "zh"
    if latin / total >= RATIO_THRESHOLD:
        return "en"
    return None


@dataclass(frozen=True, slots=True)
class VoicePlan:
    """本轮播报方案：驱动、音色与降级标记（FR-15.6 / FR-15.14）。"""

    driver: str = ""
    voice: str = "default"
    tts_lang: str = ""
    tts_fallback: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "driver": self.driver,
            "voice": self.voice,
            "tts_lang": self.tts_lang,
            "tts_fallback": self.tts_fallback,
        }


@dataclass(frozen=True, slots=True)
class LanguageDecision:
    """一次语言判定的结果（供编排层落库与广播）。"""

    lang: str
    changed: bool
    asr_driver: str
    asr_fallback: bool
    voice: VoicePlan

    def as_dict(self) -> dict[str, Any]:
        return {
            "lang": self.lang,
            "changed": self.changed,
            "asr_driver": self.asr_driver,
            "asr_fallback": self.asr_fallback,
            "voice": self.voice.as_dict(),
        }


class LanguageStats:
    """语言维度指标（C-158）：切换、降级、音色降级与分语言识别成功率。"""

    def __init__(self) -> None:
        self.switch_count = 0
        self.fallback_count = 0
        self.tts_fallback_count = 0
        self._asr_ok: Counter[str] = Counter()
        self._asr_total: Counter[str] = Counter()

    def record_asr(self, lang: str, ok: bool) -> None:
        self._asr_total[lang] += 1
        if ok:
            self._asr_ok[lang] += 1

    def as_dict(self) -> dict[str, Any]:
        by_lang: dict[str, dict[str, Any]] = {}
        for lang, total in sorted(self._asr_total.items()):
            ok = self._asr_ok.get(lang, 0)
            by_lang[lang] = {
                "total": total,
                "ok": ok,
                "success_rate": round(ok / total, 4) if total else 0.0,
            }
        return {
            "language_switch_count": self.switch_count,
            "language_fallback_count": self.fallback_count,
            "tts_fallback_count": self.tts_fallback_count,
            "asr_success_rate_by_lang": by_lang,
        }


class LanguageRouter:
    """按 design §7.6.2 / §7.6.3 选路：fixed / auto、防抖、驱动与音色降级。"""

    def __init__(
        self,
        *,
        mode: str = "auto",
        fixed: str = "zh",
        supported: list[str] | tuple[str, ...] = ("zh", "en"),
        fallback: str = "zh",
        switch_confirm_turns: int = 2,
        min_chars: int = 6,
        asr_routing: Mapping[str, str] | None = None,
        tts_voices: Mapping[str, Mapping[str, Any]] | None = None,
        default_asr: str = "",
        default_tts: str = "",
        driver_languages: Callable[[str], frozenset[str] | None] | None = None,
        prepare: Callable[[str], None] | None = None,
    ) -> None:
        self.mode = "fixed" if mode == "fixed" else "auto"
        self.fixed = normalize_lang(fixed) or "zh"
        self.fallback = normalize_lang(fallback) or "zh"
        self.supported = frozenset(item for item in map(normalize_lang, supported) if item)
        if self.fallback not in self.supported:
            self.supported = self.supported | {self.fallback}
        self.switch_confirm_turns = max(1, int(switch_confirm_turns))
        self.min_chars = max(0, int(min_chars))
        self.asr_routing = {
            normalize_lang(key): value for key, value in (asr_routing or {}).items()
        }
        self.tts_voices = {
            normalize_lang(key): dict(value) for key, value in (tts_voices or {}).items()
        }
        self.default_asr = default_asr
        self.default_tts = default_tts
        self._driver_languages = driver_languages
        self._prepare = prepare
        self.stats = LanguageStats()
        self._pending: str | None = None
        self._pending_count = 0
        self._current = self.fixed if self.mode == "fixed" else self.fallback
        if self._current not in self.supported:
            self._current = self.fallback

    @classmethod
    def from_settings(
        cls, settings: Settings, **kwargs: Any
    ) -> LanguageRouter:
        language = settings.language
        return cls(
            mode=language.mode,
            fixed=language.fixed,
            supported=language.supported,
            fallback=language.fallback,
            switch_confirm_turns=language.switch_confirm_turns,
            min_chars=language.min_chars,
            asr_routing=language.asr_routing,
            tts_voices=language.tts_voices,
            default_asr=settings.providers.asr.active,
            default_tts=settings.providers.tts.active,
            **kwargs,
        )

    @property
    def current(self) -> str:
        return self._current

    def effective(self, lang: str) -> str:
        normalized = normalize_lang(lang)
        return normalized if normalized in self.supported else self.fallback

    def asr_lang_hint(self) -> str:
        """下一次打开 ASR 时使用的语言：防抖候选优先，便于预连接后立即生效。"""
        return self._pending or self._current

    # ------------------------------------------------------------ 判定与防抖

    def observe(self, text: str, asr_lang: str = "") -> LanguageDecision:
        """观测一句识别结果，按防抖规则决定是否切语言（FR-15.4 / FR-15.13）。"""
        if self.mode == "fixed":
            return self.decision(self.fixed, changed=False)
        candidate = self._candidate(text, asr_lang)
        changed = False
        if candidate is None or candidate == self._current:
            # 无明确主语言或与当前一致：沿用当前 lang，不切换
            self._pending = None
            self._pending_count = 0
        elif len((text or "").strip()) < self.min_chars:
            # 短句（噪声 / 语气词）不触发切换，也不累积（FR-15.4）
            pass
        else:
            if candidate == self._pending:
                self._pending_count += 1
            else:
                self._pending = candidate
                self._pending_count = 1
                self._prepare_driver(candidate)
            if self._pending_count >= self.switch_confirm_turns:
                previous = self._current
                self._current = candidate
                changed = True
                self.stats.switch_count += 1
                self._pending = None
                self._pending_count = 0
                logger.info("language_switched", from_lang=previous, to_lang=candidate)
        decision = self.decision(self._current, changed=changed)
        if decision.asr_fallback:
            # 驱动不支持目标语言：显式降级到 fallback 语言并计数，不得静默返回错乱文本
            # （FR-15.5 / §7.6.3）
            self.note_language_fallback("驱动不支持目标语言", lang=self._current)
        return decision

    def decision(self, lang: str, *, changed: bool) -> LanguageDecision:
        driver, fallback = self.route(lang)
        # 能力缺失时本轮语言一并退回 fallback，与实际使用的驱动保持一致（§7.6.3）
        target = self.fallback if fallback else self.effective(lang)
        return LanguageDecision(
            lang=target,
            changed=changed,
            asr_driver=driver,
            asr_fallback=fallback,
            voice=self.voice_plan(target),
        )

    def _candidate(self, text: str, asr_lang: str) -> str | None:
        normalized = normalize_lang(asr_lang)
        # 方言无法由文字脚本判定：ASR 声明的方言优先（§7.6.2）
        if normalized in self.supported and normalized in DIALECTS:
            return normalized
        script = script_lang(text)
        if script is not None and script in self.supported:
            return script
        if normalized in self.supported:
            return normalized
        return None

    # ------------------------------------------------------------ 选路与音色

    def route(self, lang: str) -> tuple[str, bool]:
        """返回 (ASR 驱动名, 是否因能力缺失降级到 fallback)（§7.6.3）。"""
        target = self.effective(lang)
        routed = self.asr_routing.get(target)
        if routed:
            return routed, False
        if self._supports(self.default_asr, target):
            return self.default_asr, False
        fallback_driver = self.asr_routing.get(self.fallback) or self.default_asr
        return fallback_driver, target != self.fallback

    def _supports(self, driver: str, lang: str) -> bool:
        if not driver or self._driver_languages is None:
            return True
        languages = self._driver_languages(driver)
        if languages is None:
            return True  # 未声明 languages 的驱动视为不限制，避免误降级
        return lang in languages

    def voice_plan(self, lang: str) -> VoicePlan:
        """按 lang 选音色；无方言音色时普通话音色 + 降级标记（FR-15.6 / FR-15.14）。"""
        target = self.effective(lang)
        fallback_cfg = self.tts_voices.get(self.fallback) or {}
        fallback_voice = str(fallback_cfg.get("voice", "default") or "default")
        fallback_driver = str(fallback_cfg.get("driver", self.default_tts) or self.default_tts)
        cfg = self.tts_voices.get(target)
        if cfg is None:
            if target == self.fallback:
                driver = str(fallback_cfg.get("driver", self.default_tts) or self.default_tts)
                return VoicePlan(
                    driver=driver,
                    voice=fallback_voice,
                    tts_lang=self.fallback,
                    tts_fallback=False,
                )
            return VoicePlan(
                driver=fallback_driver,
                voice=fallback_voice,
                tts_lang=self.fallback,
                tts_fallback=True,
            )
        if target != self.fallback and bool(cfg.get("fallback_marked")):
            return VoicePlan(
                driver=fallback_driver,
                voice=fallback_voice,
                tts_lang=self.fallback,
                tts_fallback=True,
            )
        driver = str(cfg.get("driver", self.default_tts) or self.default_tts)
        voice = str(cfg.get("voice", "default") or "default")
        return VoicePlan(driver=driver, voice=voice, tts_lang=target, tts_fallback=False)

    def _prepare_driver(self, lang: str) -> None:
        """防抖期间预连接目标驱动，跨驱动切换后首响不劣于 NFR-1（FR-15.4）。"""
        if self._prepare is None:
            return
        driver, fallback = self.route(lang)
        current_driver, _ = self.route(self._current)
        if fallback or not driver or driver == current_driver:
            return
        try:
            self._prepare(driver)
        except Exception:
            logger.warning("language_prepare_failed", driver=driver)

    # ------------------------------------------------------------ 运行时开关

    def set_mode(self, mode: str | None = None, lang: str | None = None) -> dict[str, Any]:
        """运维切换 fixed / auto 或直接切语言（T-2A.7 / FR-15.9）。"""
        if mode in ("fixed", "auto"):
            self.mode = mode
        if lang:
            target = self.effective(lang)
            if self.mode == "fixed":
                self.fixed = target
            if target != self._current:
                self._current = target
                self.stats.switch_count += 1
                logger.info("language_mode_switch", mode=self.mode, to_lang=target)
        elif self.mode == "fixed":
            self._current = self.fixed if self.fixed in self.supported else self.fallback
        self._pending = None
        self._pending_count = 0
        return self.snapshot()

    def reset(self) -> None:
        self._current = self.fixed if self.mode == "fixed" else self.fallback
        if self._current not in self.supported:
            self._current = self.fallback
        self._pending = None
        self._pending_count = 0

    # ------------------------------------------------------------ 指标

    def note_asr_result(self, lang: str, ok: bool) -> None:
        self.stats.record_asr(self.effective(lang or self._current), ok)

    def note_language_fallback(self, reason: str, lang: str = "") -> None:
        self.stats.fallback_count += 1
        logger.warning("language_fallback", lang=lang or self._current, reason=reason)

    def note_tts_fallback(self, lang: str) -> None:
        self.stats.tts_fallback_count += 1
        logger.warning(
            "tts_voice_fallback",
            lang=lang,
            tts_lang=self.fallback,
            reason="缺少该语言音色，降级普通话并标记",
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "current": self._current,
            "fixed": self.fixed,
            "fallback": self.fallback,
            "supported": sorted(self.supported),
            "pending": self._pending,
            "pending_count": self._pending_count,
            "stats": self.stats.as_dict(),
        }


__all__ = [
    "DIALECTS",
    "LANG_NAMES",
    "LanguageDecision",
    "LanguageRouter",
    "LanguageStats",
    "VoicePlan",
    "normalize_lang",
    "script_lang",
]
