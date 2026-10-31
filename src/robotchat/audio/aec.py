"""AEC（T-1.6）：NumPy 分块频域 NLMS 软件回声消除 + `auto` 模式选择（A4、CON-13）。

设计要点（design §6.2）：
- 硬件 AEC 优先；无硬件时自动启用软件 NLMS（`audio.aec.mode=auto`）
- 软件 AEC 以「已播放音频」为参考，从采集信号中减除回声
- 双讲检测：用户说话时冻结自适应，避免近端语音把滤波器带偏
- 不引入 scipy / speex（ADR-06），只用 numpy
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np

from robotchat.observability.logging import get_logger

logger = get_logger("robotchat.audio.aec")

AecMode = Literal["hardware", "software", "none"]

_EPS = 1e-8


@dataclass(frozen=True, slots=True)
class AecCapabilities:
    """机型能力（来自 Profile：`capabilities.hardware_aec` / `echo_reference`）。"""

    hardware_aec: bool = False
    echo_reference: bool = True


class EchoCanceller(Protocol):
    def process(self, mic: np.ndarray, reference: np.ndarray) -> np.ndarray: ...

    def reset(self) -> None: ...

    def erle_db(self) -> float:
        """回声抑制量（ERLE），用于 /api/health 与自检核对（C-170）。"""


class NullEchoCanceller:
    """参考不可用或使用硬件 AEC 时的直通实现。"""

    def process(self, mic: np.ndarray, reference: np.ndarray) -> np.ndarray:
        return np.asarray(mic, dtype=np.float32)

    def reset(self) -> None:
        return None

    def erle_db(self) -> float:
        return 0.0


class BlockNlmsEchoCanceller:
    """分块时域 NLMS 回声消除（NumPy 实现，块内向量化、每块一次自适应）。

    每块流程：y = w ⊛ x（回采参考）→ e = mic - y → 梯度 g = Σ e·x → w += μ·g/‖x‖²。
    滤波器长度默认 4×块长（20ms 帧 → 80ms 回声路径），双讲时冻结自适应。
    """

    def __init__(
        self,
        frame_size: int = 320,
        sample_rate: int = 16000,
        mu: float = 0.08,
        filter_length: int | None = None,
        double_talk_ratio: float = 2.0,
        warmup_blocks: int = 25,
        reference_delay: int = 0,
    ) -> None:
        if frame_size <= 0:
            raise ValueError(f"frame_size 必须为正数：{frame_size}")
        self.frame_size = frame_size
        self.sample_rate = sample_rate
        self.mu = mu
        self.filter_length = filter_length or 4 * frame_size
        self.double_talk_ratio = double_talk_ratio
        self.warmup_blocks = warmup_blocks
        self.reference_delay = max(0, reference_delay)

        self._taps = np.zeros(self.filter_length, dtype=np.float64)
        self._history = np.zeros(self.filter_length, dtype=np.float64)
        self._delay_line = np.zeros(self.reference_delay, dtype=np.float64)
        self._erle_in = 0.0
        self._erle_out = 0.0
        self._double_talk_blocks = 0
        self._blocks = 0

    # ------------------------------------------------------------ 处理

    def process(self, mic: np.ndarray, reference: np.ndarray) -> np.ndarray:
        mic_data = np.asarray(mic, dtype=np.float64).ravel()
        ref_data = np.asarray(reference, dtype=np.float64).ravel()
        size = mic_data.size
        if size == 0:
            return mic_data.astype(np.float32)
        if ref_data.size != size:
            ref_data = self._align_reference(ref_data, size)
        if self.reference_delay:
            ref_data = np.concatenate((self._delay_line, ref_data))[:size]
            self._delay_line = ref_data[-self.reference_delay :]

        output = np.empty(size, dtype=np.float64)
        block = self.frame_size
        for start in range(0, size, block):
            stop = min(start + block, size)
            mic_block = mic_data[start:stop]
            ref_block = ref_data[start:stop]
            if mic_block.size < block:
                pad = block - mic_block.size
                mic_block = np.pad(mic_block, (0, pad))
                ref_block = np.pad(ref_block, (0, pad))
            cleaned = self._process_block(mic_block, ref_block)
            output[start:stop] = cleaned[: stop - start]
        return output.astype(np.float32)

    def _process_block(self, mic: np.ndarray, reference: np.ndarray) -> np.ndarray:
        length = self.filter_length
        extended = np.concatenate((self._history, reference))
        filtered = np.convolve(self._taps, extended)[length : length + reference.size]
        error = mic - filtered

        mic_energy = float(np.mean(mic**2))
        echo_energy = float(np.mean(filtered**2))
        # 冷启动阶段估计回声近似为 0，不能据此判定双讲（否则滤波器永不收敛）；
        # 预热结束后再启用双讲冻结，保护已收敛的滤波器不被近端语音带偏
        double_talk = (
            self._blocks >= self.warmup_blocks
            and echo_energy > _EPS
            and mic_energy > self.double_talk_ratio * echo_energy
        )
        self._blocks += 1

        if not double_talk:
            gradient = np.convolve(extended, error[::-1])[
                reference.size : reference.size + length
            ][::-1]
            # 归一化因子取「单个输出样本的输入能量窗」（L × 平均功率），
            # 块内梯度已对 N 个样本求和，除以它等价于逐样本 NLMS 的累积更新
            norm = length * float(np.mean(extended**2)) + _EPS
            self._taps += self.mu * gradient / norm
        else:
            self._double_talk_blocks += 1

        self._history = extended[-length:]
        self._erle_in += float(np.sum(mic**2))
        self._erle_out += float(np.sum(error**2))
        return error

    def _align_reference(self, reference: np.ndarray, size: int) -> np.ndarray:
        """参考与采集等长对齐：不足补零，过长取尾部（最近播放的一段）。"""
        if reference.size > size:
            return reference[-size:]
        return np.pad(reference, (size - reference.size, 0))

    # ------------------------------------------------------------ 状态

    def reset(self) -> None:
        self._taps = np.zeros(self.filter_length, dtype=np.float64)
        self._history = np.zeros(self.filter_length, dtype=np.float64)
        self._delay_line = np.zeros(self.reference_delay, dtype=np.float64)
        self._erle_in = 0.0
        self._erle_out = 0.0
        self._double_talk_blocks = 0
        self._blocks = 0

    def erle_db(self) -> float:
        if self._erle_in <= _EPS or self._erle_out <= _EPS:
            return 0.0
        return float(10.0 * np.log10(self._erle_in / self._erle_out))

    @property
    def double_talk_blocks(self) -> int:
        return self._double_talk_blocks


# ---------------------------------------------------------------- 模式选择


def resolve_aec_mode(
    mode: str, capabilities: AecCapabilities, fallback_notice: bool = True
) -> tuple[AecMode, str]:
    """返回 (实际生效模式, 说明)；`auto` 的选择结果须在健康接口与日志可见（CON-13）。"""
    if mode == "auto":
        if capabilities.hardware_aec:
            return "hardware", "Profile 声明硬件 AEC，启用 hardware"
        if capabilities.echo_reference:
            return "software", "无硬件 AEC，自动启用软件 NLMS"
        return "none", "无硬件 AEC 且无回采参考，AEC 关闭"

    if mode == "hardware":
        if capabilities.hardware_aec:
            return "hardware", "显式 hardware"
        fallback: AecMode = "software" if capabilities.echo_reference else "none"
        detail = "Profile 未声明 hardware_aec，hardware 不可用，已降级"
        if fallback_notice:
            logger.warning("aec_mode_downgraded", requested="hardware", actual=fallback)
        return fallback, detail

    if mode == "software":
        if capabilities.echo_reference:
            return "software", "显式 software"
        if fallback_notice:
            logger.warning("aec_mode_downgraded", requested="software", actual="none")
        return "none", "无回采参考，software 不可用，AEC 关闭"

    return "none", "显式关闭 AEC"


def create_echo_canceller(
    mode: AecMode, frame_size: int = 320, sample_rate: int = 16000
) -> EchoCanceller:
    if mode == "software":
        return BlockNlmsEchoCanceller(frame_size=frame_size, sample_rate=sample_rate)
    return NullEchoCanceller()
