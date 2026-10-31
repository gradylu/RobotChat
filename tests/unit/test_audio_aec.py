"""T-1.6 AEC 单测：软件 NLMS 收敛（ERLE）、双讲冻结自适应、auto 模式选择。"""

from __future__ import annotations

import numpy as np

from robotchat.audio.aec import (
    AecCapabilities,
    BlockNlmsEchoCanceller,
    NullEchoCanceller,
    create_echo_canceller,
    resolve_aec_mode,
)

BLOCK = 320
RATE = 16000


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


def test_nlms_cancels_echo() -> None:
    reference, mic = _echo_scenario()
    aec = BlockNlmsEchoCanceller(frame_size=BLOCK)
    residual = np.concatenate(
        [
            aec.process(mic[start : start + BLOCK], reference[start : start + BLOCK])
            for start in range(0, reference.size, BLOCK)
        ]
    )
    # 设计 §6.2 / C-170：收敛后 ERLE ≥ 15 dB
    assert _tail_erle(mic, residual) >= 15.0
    assert aec.erle_db() >= 10.0
    assert aec.double_talk_blocks == 0


def test_double_talk_freezes_adaptation() -> None:
    reference, mic = _echo_scenario(blocks=200, seed=1)
    aec = BlockNlmsEchoCanceller(frame_size=BLOCK)

    warmup = BLOCK * 120
    for start in range(0, warmup, BLOCK):
        aec.process(mic[start : start + BLOCK], reference[start : start + BLOCK])
    assert aec.double_talk_blocks == 0

    speech = 0.5 * np.sin(2 * np.pi * 300 * np.arange(BLOCK * 80) / RATE)
    for start in range(0, BLOCK * 80, BLOCK):
        block = slice(start, start + BLOCK)
        aec.process(
            mic[warmup + start : warmup + start + BLOCK] + speech[block],
            reference[warmup + start : warmup + start + BLOCK],
        )
    assert aec.double_talk_blocks > 0


def test_resolve_aec_mode_auto() -> None:
    assert resolve_aec_mode("auto", AecCapabilities(hardware_aec=True))[0] == "hardware"
    assert resolve_aec_mode("auto", AecCapabilities(echo_reference=True))[0] == "software"
    assert resolve_aec_mode("auto", AecCapabilities(echo_reference=False))[0] == "none"


def test_resolve_aec_mode_downgrades_when_capability_missing() -> None:
    mode, _ = resolve_aec_mode(
        "hardware", AecCapabilities(echo_reference=True), fallback_notice=False
    )
    assert mode == "software"

    mode, _ = resolve_aec_mode(
        "software", AecCapabilities(echo_reference=False), fallback_notice=False
    )
    assert mode == "none"

    assert resolve_aec_mode("none", AecCapabilities())[0] == "none"


def test_create_echo_canceller() -> None:
    assert isinstance(create_echo_canceller("software"), BlockNlmsEchoCanceller)
    assert isinstance(create_echo_canceller("hardware"), NullEchoCanceller)
    passthrough = create_echo_canceller("none")
    samples = np.zeros(4, dtype=np.float32)
    assert passthrough.process(samples, samples).shape == (4,)
    assert passthrough.erle_db() == 0.0
