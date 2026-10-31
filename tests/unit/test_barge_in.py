"""T-4.1 / T-4.2 / T-4.6 / T-4.8 打断检测器单测（C-02、C-07、C-10、C-11、C-169）。

覆盖：噪声不触发、真实语音多帧触发、保护窗口、语义回声剔除、语义确认、超时兜底、
`mode=disabled` 旁路、参数热更新与保护窗口随 AEC 模式自适应。
"""

from __future__ import annotations

from robotchat.audio.barge_in import (
    DEFAULT_GUARD_MS,
    BargeInDetector,
    resolve_guard_ms,
)
from robotchat.config.schema import BargeInSettings


class FakeClock:
    """可控单调时钟：直接推进毫秒，避免测试依赖真实等待。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, ms: float) -> None:
        self.now += ms / 1000.0


def _warm_floor(detector: BargeInDetector, clock: FakeClock, level_db: float = -56.0) -> None:
    """非播报期喂足噪声帧，让自适应噪声底收敛到给定水平。"""
    for _ in range(20):
        detector.observe_frame(level_db, ts=clock())
        clock.advance_ms(20)


def test_guard_ms_adapts_to_aec_mode_and_config_override() -> None:
    assert resolve_guard_ms(BargeInSettings(), "hardware") == 100
    assert resolve_guard_ms(BargeInSettings(), "software") == 200
    assert resolve_guard_ms(BargeInSettings(), "none") == 300
    assert resolve_guard_ms(BargeInSettings(guard_ms=50), "software") == 50
    assert DEFAULT_GUARD_MS == {"hardware": 100, "software": 200, "none": 300}


def test_noise_does_not_trigger_barge_in() -> None:
    clock = FakeClock()
    detector = BargeInDetector(
        BargeInSettings(threshold_boost_db=6.0, confirm_frames=3), "software", clock=clock
    )
    _warm_floor(detector, clock, -56.0)

    detector.mark_playback_start(ts=clock())
    clock.advance_ms(260)  # 已越过 200ms 保护窗口
    for _ in range(10):
        # 噪声低于「噪声底 -56 + 6 = -50」，不满足能量条件
        assert detector.observe_frame(-53.0, ts=clock()) is None
        clock.advance_ms(20)
    assert detector.accepted == 0


def test_guard_window_blocks_early_speech() -> None:
    clock = FakeClock()
    detector = BargeInDetector(
        BargeInSettings(semantic_check=False, confirm_frames=1), "software", clock=clock
    )
    _warm_floor(detector, clock, -60.0)
    detector.mark_playback_start(ts=clock())

    # 保护窗口内即使能量很高也不判定（C4）
    for _ in range(5):
        assert detector.observe_frame(-10.0, ts=clock()) is None
        clock.advance_ms(20)
    assert detector.accepted == 0

    clock.advance_ms(150)  # 累计 250ms > 200ms
    decision = detector.observe_frame(-10.0, ts=clock())
    assert decision is not None and decision.accepted
    assert decision.trigger == "energy"


def test_speech_multi_frame_triggers_barge_in() -> None:
    clock = FakeClock()
    detector = BargeInDetector(
        BargeInSettings(semantic_check=False, confirm_frames=3), "software", clock=clock
    )
    _warm_floor(detector, clock, -56.0)
    detector.mark_playback_start(ts=clock())
    clock.advance_ms(250)

    assert detector.observe_frame(-20.0, ts=clock()) is None
    clock.advance_ms(20)
    assert detector.observe_frame(-18.0, ts=clock()) is None
    clock.advance_ms(20)
    decision = detector.observe_frame(-16.0, ts=clock())
    assert decision is not None
    assert decision.accepted and decision.trigger == "energy"
    assert detector.accepted == 1
    # 同一次播报内只放行一次
    assert detector.observe_frame(-16.0, ts=clock()) is None


def test_semantic_echo_is_suppressed() -> None:
    clock = FakeClock()
    detector = BargeInDetector(BargeInSettings(), "software", clock=clock)
    _warm_floor(detector, clock, -56.0)
    detector.note_spoken_text("今天天气不错，适合出门散步。")
    detector.mark_playback_start(ts=clock())
    clock.advance_ms(250)

    assert detector.observe_frame(-20.0, ts=clock()) is None
    clock.advance_ms(20)
    assert detector.observe_frame(-20.0, ts=clock()) is None
    clock.advance_ms(20)
    pending = detector.observe_frame(-20.0, ts=clock())
    # 达到 confirm_frames 后进入待语义确认，不直接放行
    assert pending is not None and pending.pending and not pending.accepted

    decision = detector.observe_partial("今天天气不错适合出门散步", ts=clock())
    assert decision is not None
    assert decision.echo_suppressed and not decision.accepted
    assert detector.echo_suppressed == 1
    assert detector.accepted == 0


def test_semantic_confirmation_accepts_different_text() -> None:
    clock = FakeClock()
    detector = BargeInDetector(BargeInSettings(), "software", clock=clock)
    _warm_floor(detector, clock, -56.0)
    detector.note_spoken_text("今天天气不错。")
    detector.mark_playback_start(ts=clock())
    clock.advance_ms(250)

    pending = None
    for _ in range(3):
        pending = detector.observe_frame(-20.0, ts=clock())
        clock.advance_ms(20)
    assert pending is not None and pending.pending and not pending.accepted

    decision = detector.observe_partial("帮我打开空调", ts=clock())
    assert decision is not None and decision.accepted
    assert decision.trigger == "semantic"
    assert detector.accepted == 1


def test_pending_semantic_times_out_to_energy_decision() -> None:
    clock = FakeClock()
    detector = BargeInDetector(BargeInSettings(), "software", clock=clock)
    _warm_floor(detector, clock, -56.0)
    detector.mark_playback_start(ts=clock())
    clock.advance_ms(250)
    for _ in range(3):
        detector.observe_frame(-20.0, ts=clock())
        clock.advance_ms(20)

    # ASR 不可用时不得永久挂起：超时后按能量判定放行
    clock.advance_ms(700)
    decision = detector.observe_frame(-20.0, ts=clock())
    assert decision is not None and decision.accepted
    assert decision.trigger == "vad"


def test_disabled_mode_bypasses_detection() -> None:
    clock = FakeClock()
    detector = BargeInDetector(BargeInSettings(mode="disabled"), "software", clock=clock)
    assert detector.enabled is False
    _warm_floor(detector, clock, -56.0)
    detector.mark_playback_start(ts=clock())
    clock.advance_ms(500)
    for _ in range(10):
        assert detector.observe_frame(0.0, ts=clock()) is None
    assert detector.observe_partial("任意话语") is None
    assert detector.accepted == 0


def test_hot_update_applies_immediately() -> None:
    clock = FakeClock()
    detector = BargeInDetector(BargeInSettings(semantic_check=False), "software", clock=clock)
    _warm_floor(detector, clock, -56.0)

    detector.apply_settings(BargeInSettings(guard_ms=50, semantic_check=False))
    assert detector.guard_ms == 50
    assert detector.snapshot()["guard_ms"] == 50

    detector.apply_settings(BargeInSettings(mode="disabled"), aec_mode="none")
    assert detector.enabled is False
    assert detector.snapshot()["aec_mode"] == "none"
    assert detector.guard_ms == 300

    detector.apply_settings(BargeInSettings(mode="enabled", semantic_check=False))
    assert detector.enabled is True


def test_reset_clears_noise_floor_and_echo_reference() -> None:
    clock = FakeClock()
    detector = BargeInDetector(BargeInSettings(), "software", clock=clock)
    _warm_floor(detector, clock, -56.0)
    detector.note_spoken_text("今天天气不错。")

    detector.reset()
    assert detector.snapshot()["noise_floor_db"] == -60.0

    detector.note_spoken_text("打开导航")
    detector.mark_playback_start(ts=clock())
    clock.advance_ms(250)
    for _ in range(2):
        detector.observe_frame(-20.0, ts=clock())
        clock.advance_ms(20)
    pending = detector.observe_frame(-20.0, ts=clock())
    assert pending is not None and pending.pending

    # reset 已清空旧播报文本，只有「打开导航」参与比对，旧文本不被判为回声
    decision = detector.observe_partial("今天天气不错", ts=clock())
    assert decision is not None and decision.accepted
