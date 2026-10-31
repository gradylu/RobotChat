"""AEC 双 Profile 与打断联动契约（T-4.9 / C-169、C-170）。

覆盖要点：
- 两种机型 Profile 下 `auto` 模式选择正确（有硬件 → hardware；有回采无硬件 → software；
  无回采 → none），且模式切换在快照中可见；
- 保护窗口随 AEC 实际模式自适应（hardware 100 / software 200 / none 300 ms）；
- 软件 NLMS 收敛后回声衰减（ERLE）≥ 15 dB；
- 播报期 AEC 残留能量不足以触发打断，且与播报文本高度相似的回声被剔除。
"""

from __future__ import annotations

import numpy as np

from robotchat.audio.aec import (
    AecCapabilities,
    BlockNlmsEchoCanceller,
    resolve_aec_mode,
)
from robotchat.audio.barge_in import DEFAULT_GUARD_MS, BargeInDetector, resolve_guard_ms
from robotchat.config.schema import BargeInSettings

from .conftest import BASE_PROFILE, SECOND_PROFILE, make_profile

BLOCK = 320
RATE = 16000


class _Clock:
    """可控时钟：帧判定与保护窗口均按毫秒推进。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, ms: float) -> None:
        self.now += ms / 1000.0


def _capabilities(raw: dict[str, object]) -> AecCapabilities:
    profile = make_profile(raw)
    caps = profile.capabilities
    return AecCapabilities(
        hardware_aec=caps.hardware_aec, echo_reference=caps.echo_reference
    )


def test_auto_mode_selects_per_profile() -> None:
    # 契约 Profile A：有回采参考、无硬件 → software
    assert resolve_aec_mode("auto", _capabilities(BASE_PROFILE))[0] == "software"
    # 契约 Profile B：无回采参考、无硬件 → none
    assert resolve_aec_mode("auto", _capabilities(SECOND_PROFILE))[0] == "none"
    # 硬件 AEC 机型（硬件必须同时具备回采参考）→ hardware
    hardware = {**BASE_PROFILE, "capabilities": {**BASE_PROFILE["capabilities"]}}
    hardware["capabilities"]["hardware_aec"] = True
    assert resolve_aec_mode("auto", _capabilities(hardware))[0] == "hardware"


def test_guard_window_follows_resolved_mode() -> None:
    settings = BargeInSettings()
    assert resolve_guard_ms(settings, "hardware") == 100
    assert resolve_guard_ms(settings, "software") == 200
    assert resolve_guard_ms(settings, "none") == 300
    # 显式配置覆盖自适应默认值
    assert resolve_guard_ms(BargeInSettings(guard_ms=50), "hardware") == 50
    assert DEFAULT_GUARD_MS == {"hardware": 100, "software": 200, "none": 300}


def _echo_scenario(blocks: int = 300, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """仿真回声路径（衰减 + 20/40 样本两处反射）+ 低幅近端噪声。"""
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(BLOCK * blocks)
    reference = np.convolve(noise, np.ones(8) / 8, mode="same")
    reference /= np.max(np.abs(reference)) * 2.0

    path = np.zeros(64)
    path[0] = 0.2
    path[20] = 0.5
    path[40] = 0.2
    echo = np.convolve(reference, path)[: reference.size]
    mic = echo + 0.001 * rng.standard_normal(reference.size)
    return reference, mic


def _tail_erle(mic: np.ndarray, residual: np.ndarray, blocks: int = 50) -> float:
    tail = slice(-BLOCK * blocks, None)
    echo_energy = float(np.mean(mic[tail] ** 2))
    residual_energy = max(float(np.mean(residual[tail] ** 2)), 1e-30)
    return float(10.0 * np.log10(echo_energy / residual_energy))


def _warm_floor(detector: BargeInDetector, clock: _Clock, level_db: float = -56.0) -> None:
    """非播报期灌入噪声帧，让自适应噪声底收敛。"""
    for _ in range(50):
        detector.observe_frame(level_db)
        clock.advance_ms(20)


def test_software_aec_reaches_erle_target() -> None:
    # C-170：software 模式回声衰减 ≥ 15 dB
    reference, mic = _echo_scenario()
    aec = BlockNlmsEchoCanceller(frame_size=BLOCK)
    residual = np.concatenate(
        [
            aec.process(mic[start : start + BLOCK], reference[start : start + BLOCK])
            for start in range(0, reference.size, BLOCK)
        ]
    )
    assert _tail_erle(mic, residual) >= 15.0


def test_residual_echo_does_not_trigger_barge_in_for_both_profiles() -> None:
    """两种 Profile 下，播报期 AEC 残留能量都不得被判为打断（C-169）。"""
    for raw in (BASE_PROFILE, SECOND_PROFILE):
        mode, _ = resolve_aec_mode("auto", _capabilities(raw))
        clock = _Clock()
        detector = BargeInDetector(BargeInSettings(), aec_mode=mode, clock=clock)
        _warm_floor(detector, clock)
        detector.note_spoken_text("今天天气不错，适合出门。")
        detector.mark_playback_start()

        # 越过保护窗口后灌入残留回声（低于噪声底 + 抬升门限）→ 不得产生结论文
        clock.advance_ms(resolve_guard_ms(detector.settings, mode) + 20)
        for _ in range(10):
            assert detector.observe_frame(-53.0) is None
            clock.advance_ms(20)
        assert detector.accepted == 0
        assert detector.pending == 0

        # 残留回声被 ASR 识别为播报原文时，语义层应判为回声丢弃
        detector.observe_frame(-40.0)
        detector.observe_frame(-40.0)
        pending = detector.observe_frame(-40.0)
        assert pending is not None and pending.pending and not pending.accepted
        echo = detector.observe_partial("今天天气不错")
        assert echo is not None and echo.echo_suppressed and not echo.accepted
        assert detector.accepted == 0
        assert detector.snapshot()["aec_mode"] == mode
