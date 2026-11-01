"""端到端主链路（T-8.2 / C-150 / C-102）：本地 WAV → VAD → ASR → LLM → TTS → WAV。

全部外部服务为本地桩件，不接硬件、不连网络；验证轮次落库、首响指标与多轮记忆。
"""

from __future__ import annotations

from pathlib import Path

from conftest import (
    FakeASRProvider,
    FakeLLM,
    FakeTTS,
    ScriptedVad,
    make_orchestrator,
    make_settings,
    wait_turn,
    wav_frames,
    write_wav,
)

from robotchat.audio.file_io import FileAudioTransport
from robotchat.core.state_machine import State
from robotchat.observability.metrics import (
    ASR_FINAL,
    E2E_FIRST_RESPONSE,
    LLM_FIRST_TOKEN,
    TTS_FIRST_CHUNK,
)


async def test_file_audio_runs_full_turn_and_records_timings(tmp_path: Path) -> None:
    """T-8.2 / C-150：本地 WAV 驱动一轮完整对话，轮次与首响指标全部可见。"""
    settings = make_settings(tmp_path)
    capture = write_wav(tmp_path / "in.wav", 30)
    playback = tmp_path / "out.wav"
    transport = FileAudioTransport(
        {"capture_path": str(capture), "playback_path": str(playback), "realtime": False}
    )
    asr = FakeASRProvider(["今天天气怎么样？"], final_lang="zh")
    llm = FakeLLM(["你好。", "我是", "机器人助手。"])
    tts = FakeTTS()
    orchestrator, store = make_orchestrator(
        settings,
        tmp_path,
        transport=transport,
        vad=ScriptedVad(start_seq=3, end_seq=20),
        providers={"asr": asr, "llm": llm, "tts": tts},
    )
    await orchestrator.start()
    assert orchestrator.machine.state == State.ACTIVE_IDLE

    turn = await wait_turn(store, 1)
    assert turn["status"] == "completed"
    assert turn["user_text"] == "今天天气怎么样？"
    assert turn["assistant_text"] == "你好。我是机器人助手。"
    timings = turn["timings"]
    assert {"llm_first", "tts_first", "e2e"} <= set(timings)
    assert all(value >= 0 for value in timings.values())

    # 逐句合成：两句文本分别送入 TTS，音频块经播放队列落盘
    assert [request.text for request in tts.requests] == ["你好。", "我是机器人助手。"]
    assert asr.total_frames > 0
    assert transport.played_chunks >= 2

    snapshot = orchestrator.metrics.snapshot()
    for stage in (ASR_FINAL, LLM_FIRST_TOKEN, TTS_FIRST_CHUNK, E2E_FIRST_RESPONSE):
        assert snapshot[stage]["count"] >= 1
    assert orchestrator.machine.state == State.ACTIVE_IDLE

    await orchestrator.shutdown()
    assert wav_frames(playback) > 0


async def test_second_turn_sees_previous_exchange_in_memory(tmp_path: Path) -> None:
    """T-8.2 / C-150：多轮记忆，第二轮 ChatRequest 携带第一轮用户与助手内容。"""
    settings = make_settings(tmp_path)
    llm = FakeLLM(["好的，", "小明。"])
    tts = FakeTTS()
    orchestrator, store = make_orchestrator(
        settings, tmp_path, providers={"llm": llm, "tts": tts}
    )
    await orchestrator.start()

    first = await orchestrator.handle_text("我叫小明")
    assert first is not None
    assert first["assistant_text"] == "好的，小明。"
    second = await orchestrator.handle_text("我叫什么？")
    assert second is not None

    assert store.turn("default", 2)["status"] == "completed"
    assert store.turn("default", 2)["assistant_text"] == "好的，小明。"
    request = llm.requests[1]
    contents = [message.content for message in request.messages]
    assert "我叫小明" in contents
    assert "好的，小明。" in contents
    assert request.messages[-1].content == "我叫什么？"
    assert orchestrator.memory_snapshot()["history"] == 4

    await orchestrator.shutdown()
