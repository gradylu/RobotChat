"""语言路由单测：fixed / auto 防抖、混说占比、选路降级与音色标记（T-2A.2 / T-2A.5 / T-2A.6）。"""

from __future__ import annotations

from robotchat.dialog.language import (
    LanguageRouter,
    LanguageStats,
    normalize_lang,
    script_lang,
)


def test_normalize_lang_aliases() -> None:
    assert normalize_lang("zh-CN") == "zh"
    assert normalize_lang("en_US") == "en"
    assert normalize_lang("cantonese") == "yue"
    assert normalize_lang("yue-hk") == "yue"
    assert normalize_lang("") == ""


def test_script_lang_ratio_threshold() -> None:
    assert script_lang("今天天气不错，我们出去走走吧") == "zh"
    assert script_lang("Good morning, this is a test message") == "en"
    # 中英各半：无法判定，沿用当前语言
    assert script_lang("你好世界 hello") is None
    assert script_lang("") is None


def test_fixed_mode_never_switches() -> None:
    router = LanguageRouter(mode="fixed", fixed="en", supported=["zh", "en"])
    decision = router.observe("今天天气不错，我们出去走走吧", asr_lang="zh")
    assert decision.lang == "en"
    assert decision.changed is False
    assert router.current == "en"


def test_auto_switches_after_confirm_turns() -> None:
    router = LanguageRouter(
        mode="auto",
        supported=["zh", "en"],
        fallback="zh",
        switch_confirm_turns=2,
        min_chars=6,
    )
    first = router.observe("good morning everyone", asr_lang="en")
    assert first.lang == "zh"
    assert first.changed is False
    assert router.asr_lang_hint() == "en"  # 防抖候选优先，便于预连接

    second = router.observe("good morning everyone", asr_lang="en")
    assert second.lang == "en"
    assert second.changed is True
    assert router.current == "en"
    assert router.stats.switch_count == 1


def test_short_utterance_does_not_switch() -> None:
    router = LanguageRouter(
        mode="auto", supported=["zh", "en"], fallback="zh", switch_confirm_turns=1, min_chars=6
    )
    for _ in range(3):
        router.observe("hi", asr_lang="en")
    assert router.current == "zh"


def test_dialect_declared_by_asr_wins_over_script() -> None:
    router = LanguageRouter(
        mode="auto",
        supported=["zh", "yue"],
        fallback="zh",
        switch_confirm_turns=1,
        min_chars=6,
    )
    decision = router.observe("今日天气唔错，出去行下街", asr_lang="yue")
    assert decision.lang == "yue"
    assert decision.changed is True


def test_route_prefers_routing_then_capabilities() -> None:
    languages = {"funasr_local": frozenset({"zh", "en"}), "funasr_dialect": frozenset({"dongbei"})}
    router = LanguageRouter(
        mode="auto",
        supported=["zh", "en", "dongbei", "fr"],
        fallback="zh",
        asr_routing={"dongbei": "funasr_dialect"},
        default_asr="funasr_local",
        driver_languages=lambda name: languages.get(name),
    )
    assert router.route("dongbei") == ("funasr_dialect", False)
    assert router.route("en") == ("funasr_local", False)
    # 未声明能力的语言：退回 fallback 驱动并标记降级
    assert router.route("fr") == ("funasr_local", True)


def test_voice_plan_marks_dialect_fallback() -> None:
    router = LanguageRouter(
        mode="auto",
        supported=["zh", "yue", "dongbei"],
        fallback="zh",
        default_tts="qwen3_tts_local",
        tts_voices={
            "zh": {"driver": "qwen3_tts_local", "voice": "default"},
            "yue": {"driver": "dashscope_tts", "voice": "yue_female"},
            "dongbei": {"driver": "qwen3_tts_local", "voice": "default", "fallback_marked": True},
        },
    )
    yue = router.voice_plan("yue")
    assert yue.driver == "dashscope_tts"
    assert yue.voice == "yue_female"
    assert yue.tts_lang == "yue"
    assert yue.tts_fallback is False

    dongbei = router.voice_plan("dongbei")
    assert dongbei.voice == "default"
    assert dongbei.tts_lang == "zh"
    assert dongbei.tts_fallback is True


def test_voice_plan_without_config_falls_back() -> None:
    router = LanguageRouter(
        mode="auto", supported=["zh", "wuhan"], fallback="zh", default_tts="qwen3_tts_local"
    )
    plan = router.voice_plan("wuhan")
    assert plan.driver == "qwen3_tts_local"
    assert plan.tts_lang == "zh"
    assert plan.tts_fallback is True


def test_set_mode_and_reset() -> None:
    router = LanguageRouter(mode="auto", supported=["zh", "en"], fallback="zh")
    router.set_mode(lang="en")
    assert router.mode == "auto" and router.current == "en"

    router.set_mode(mode="fixed", lang="zh")
    assert router.mode == "fixed" and router.current == "zh"

    router.reset()
    assert router.current == "zh"


def test_language_stats_shape() -> None:
    stats = LanguageStats()
    stats.record_asr("zh", True)
    stats.record_asr("en", False)
    stats.switch_count = 1
    stats.fallback_count = 2
    stats.tts_fallback_count = 3
    payload = stats.as_dict()
    assert payload["language_switch_count"] == 1
    assert payload["language_fallback_count"] == 2
    assert payload["tts_fallback_count"] == 3
    assert payload["asr_success_rate_by_lang"]["zh"]["success_rate"] == 1.0
    assert payload["asr_success_rate_by_lang"]["en"]["success_rate"] == 0.0
