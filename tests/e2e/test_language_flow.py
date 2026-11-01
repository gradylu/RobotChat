"""多语种与方言端到端流程（T-8.2 / C-150 ~ C-156）。

全离线桩件：ASR / LLM / TTS 全部脚本化，校验语言路由、`lang` / `tts_lang` /
`tts_fallback` 落库与降级告警；不接硬件、不连网络。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import (
    FakeASRProvider,
    FakeLLM,
    FakeTTS,
    make_orchestrator,
    make_settings,
    wait_turn,
    wait_until,
)

from robotchat.config.schema import Settings
from robotchat.core.orchestrator import DialogOrchestrator
from robotchat.core.state_machine import State
from robotchat.dialog.session import SessionStore
from robotchat.providers.registry import ProviderRegistry


def _settings(tmp_path: Path, extra: dict[str, str] | None = None) -> Settings:
    """离线语言用例的公共配置：关闭 Barge-in，避免播报期额外打开 ASR 热流。"""
    environ = {"ROBOTCHAT__BARGE_IN__MODE": "disabled"}
    environ.update(extra or {})
    return make_settings(tmp_path, environ)


def _payloads(store: SessionStore, event_type: str) -> list[dict[str, Any]]:
    return [item["payload"] for item in store.events("default") if item["type"] == event_type]


async def _asr_turn(
    orchestrator: DialogOrchestrator, store: SessionStore, seq: int
) -> dict[str, Any]:
    """用真实 ASR 流驱动一轮：起音 → 收音 → 等待轮次落库并回到常开聆听。"""
    await wait_until(
        lambda: orchestrator.machine.state == State.ACTIVE_IDLE
        and orchestrator._current_turn is None
        and (orchestrator._turn_task is None or orchestrator._turn_task.done())
    )
    await orchestrator._on_speech_start()
    await orchestrator._on_speech_end()
    return await wait_turn(store, seq)


async def test_mandarin_text_turn_uses_zh_voice(tmp_path: Path) -> None:
    """T-8.2 / C-150：普通话轮次落库 lang/tts_lang 为 zh，并使用普通话音色。"""
    settings = _settings(tmp_path)
    llm = FakeLLM(["今天晴，", "气温二十度。"])
    tts = FakeTTS()
    orchestrator, store = make_orchestrator(
        settings, tmp_path, providers={"llm": llm, "tts": tts}
    )
    await orchestrator.start()

    result = await orchestrator.handle_text("你好，今天天气怎么样？", lang="zh")
    assert result is not None
    assert result["assistant_text"] == "今天晴，气温二十度。"

    turn = store.turn("default", 1)
    assert turn["status"] == "completed"
    assert turn["lang"] == "zh"
    assert turn["tts_lang"] == "zh"
    assert turn["tts_fallback"] == 0

    request = llm.requests[0]
    assert request.lang == "zh"
    assert "请用简体中文回答" in request.messages[0].content
    assert tts.requests[0].voice == "default"
    assert tts.requests[0].lang == "zh"

    await orchestrator.shutdown()


async def test_english_text_turn_uses_en_voice(tmp_path: Path) -> None:
    """T-8.2 / C-151：英文轮次使用英文提示词约束与 en_female 音色。"""
    settings = _settings(tmp_path)
    llm = FakeLLM(["It is sunny today."])
    tts = FakeTTS()
    orchestrator, store = make_orchestrator(
        settings, tmp_path, providers={"llm": llm, "tts": tts}
    )
    await orchestrator.start()

    result = await orchestrator.handle_text("Hello, how is the weather today?", lang="en")
    assert result is not None
    assert result["assistant_text"] == "It is sunny today."

    turn = store.turn("default", 1)
    assert turn["lang"] == "en"
    assert turn["tts_lang"] == "en"
    assert turn["tts_fallback"] == 0

    request = llm.requests[0]
    assert request.lang == "en"
    assert "Please answer in English" in request.messages[0].content
    assert tts.requests[0].voice == "en_female"
    assert tts.requests[0].lang == "en"

    await orchestrator.shutdown()


async def test_mixed_utterance_keeps_lang_until_confirmed(tmp_path: Path) -> None:
    """T-8.2 / C-152：中英混说整句不截断；无语言占比达 70% 时沿用当前 lang。"""
    settings = _settings(tmp_path)
    asr = FakeASRProvider(
        [
            "帮我把 waypoint 3 设为目标",
            "Hello there, I need help",
            "Please set the waypoint now",
        ]
    )
    llm = FakeLLM(["明白了。"])
    tts = FakeTTS()
    orchestrator, store = make_orchestrator(
        settings, tmp_path, providers={"asr": asr, "llm": llm, "tts": tts}
    )
    await orchestrator.start()

    first = await _asr_turn(orchestrator, store, 1)
    second = await _asr_turn(orchestrator, store, 2)
    third = await _asr_turn(orchestrator, store, 3)

    # 语言占比不足 70% 的混说句整句保留，既不截断也不切换语言
    assert first["user_text"] == "帮我把 waypoint 3 设为目标"
    assert first["lang"] == "zh"
    assert second["lang"] == "zh"
    # 连续第二句英文才满足 switch_confirm_turns=2，切到英文并使用英文音色
    assert third["lang"] == "en"
    assert third["tts_lang"] == "en"
    voices = {request.turn_id: request.voice for request in tts.requests}
    assert voices[3] == "en_female"
    assert orchestrator.language.snapshot()["stats"]["language_switch_count"] == 1
    changed = [(payload["from"], payload["to"]) for payload in _payloads(store, "language.changed")]
    assert changed == [("zh", "en")]

    await orchestrator.shutdown()


async def test_dialect_asr_unavailable_degrades_with_warning(tmp_path: Path) -> None:
    """T-8.2 / C-153 / C-154：方言 ASR 不可达时降级普通话播报并记录告警。"""
    settings = _settings(
        tmp_path,
        {"ROBOTCHAT__LANGUAGE__TTS_VOICES__YUE__FALLBACK_MARKED": "true"},
    )
    registry = ProviderRegistry()
    registry.set_offline(True, "e2e 离线：云端方言驱动锁定")
    slot = registry.register_slot(
        "asr", "e2e_slot_asr", {"texts": ["帮我看看充电桩"], "final_lang": "zh"}
    )
    llm = FakeLLM(["已为你查看充电桩。"])
    tts = FakeTTS()
    orchestrator, store = make_orchestrator(
        settings,
        tmp_path,
        providers={"llm": llm, "tts": tts},
        registry=registry,
    )
    orchestrator.language.set_mode(lang="yue")
    await orchestrator.start()

    turn = await _asr_turn(orchestrator, store, 1)
    assert turn["user_text"] == "帮我看看充电桩"
    assert turn["assistant_text"] == "已为你查看充电桩。"
    # 方言路由不可用时 ASR 退回普通话音色，但轮次仍保留用户语言（C-156）
    assert turn["lang"] == "yue"
    assert turn["tts_lang"] == "zh"
    assert turn["tts_fallback"] == 1

    assert slot.primary.driver.langs == ["zh"]
    named = registry.named_snapshot()["asr/dashscope_stream"]["driver"]
    assert named["locked"] is True
    assert "锁定" in named["locked_reason"]
    fallback = _payloads(store, "language.fallback")[-1]
    assert fallback == {"from": "yue", "to": "zh", "reason": "方言 ASR 不可达"}
    assert _payloads(store, "language.tts_fallback")[-1] == {
        "lang": "yue",
        "tts_lang": "zh",
    }
    stats = orchestrator.language.snapshot()["stats"]
    assert stats["language_fallback_count"] == 1
    assert stats["tts_fallback_count"] == 1

    assert llm.requests[0].lang == "yue"
    assert "请使用普通话书面语回答" in llm.requests[0].messages[0].content
    assert tts.requests[0].voice == "default"
    assert tts.requests[0].lang == "zh"

    await orchestrator.shutdown()


async def test_session_language_switch_debounce(tmp_path: Path) -> None:
    """T-8.2 / C-155：会话内中→英→中切换，短句与单句不一致均不触发切换。"""
    settings = _settings(tmp_path)
    asr = FakeASRProvider(
        [
            "今天天气怎么样",
            "Hello, please open the door",
            "Please tell me the weather",
            "好的",
            "帮我把窗户打开",
            "我想听一首音乐",
        ]
    )
    llm = FakeLLM(["好的。"])
    tts = FakeTTS()
    orchestrator, store = make_orchestrator(
        settings, tmp_path, providers={"asr": asr, "llm": llm, "tts": tts}
    )
    await orchestrator.start()

    turns = [await _asr_turn(orchestrator, store, seq) for seq in range(1, 7)]

    assert [turn["lang"] for turn in turns] == ["zh", "zh", "en", "en", "en", "zh"]
    assert [turn["tts_lang"] for turn in turns] == ["zh", "zh", "en", "en", "en", "zh"]
    voices = {request.turn_id: request.voice for request in tts.requests}
    assert [voices[seq] for seq in range(1, 7)] == [
        "default",
        "default",
        "en_female",
        "en_female",
        "en_female",
        "default",
    ]
    assert orchestrator.language.snapshot()["stats"]["language_switch_count"] == 2
    changed = [(payload["from"], payload["to"]) for payload in _payloads(store, "language.changed")]
    assert changed == [("zh", "en"), ("en", "zh")]
    # 同一活跃期内切换不重开会话
    assert _payloads(store, "session.reset") == []

    await orchestrator.shutdown()


async def test_dialect_without_voice_falls_back_to_mandarin_tts(tmp_path: Path) -> None:
    """T-8.2 / C-154 / C-156：方言无音色时保留 lang，tts_lang 降级 zh 并标记。"""
    settings = _settings(tmp_path)
    llm = FakeLLM(["已为你查看充电桩。"])
    tts = FakeTTS()
    orchestrator, store = make_orchestrator(
        settings, tmp_path, providers={"llm": llm, "tts": tts}
    )
    await orchestrator.start()

    result = await orchestrator.handle_text("帮我看看充电桩", lang="dongbei")
    assert result is not None
    turn = store.turn("default", 1)
    assert turn["status"] == "completed"
    assert turn["lang"] == "dongbei"
    assert turn["tts_lang"] == "zh"
    assert turn["tts_fallback"] == 1

    assert llm.requests[0].lang == "dongbei"
    assert "请使用普通话书面语回答" in llm.requests[0].messages[0].content
    assert tts.requests[0].voice == "default"
    assert tts.requests[0].lang == "zh"
    assert _payloads(store, "language.tts_fallback")[-1] == {
        "lang": "dongbei",
        "tts_lang": "zh",
    }
    assert orchestrator.language.snapshot()["stats"]["tts_fallback_count"] == 1

    await orchestrator.shutdown()
