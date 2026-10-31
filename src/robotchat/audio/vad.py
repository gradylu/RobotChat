"""VAD（T-1.5）：Silero VAD ONNX + 自适应噪声底 + 起音多帧确认 + 尾点检测 + 最大时长保护。

- 模型可插拔：`silero_vad`（默认，ONNX 进程内推理，ADR-05）与 `energy_vad`（无模型环境的诊断后端）
- 判定口径：起音需连续 `vad.start_frames` 帧；尾点需静音 `vad.silence_ms`（700ms，独立计量，NFR-1）
- 最大时长保护：单句超过 `vad.max_utterance_ms` 强制端点，避免链路被长语音拖死
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from robotchat.audio.buffer import pcm16_to_float, resample_float, rms_dbfs
from robotchat.audio.frame import AudioFrame
from robotchat.config.schema import VadSettings
from robotchat.health import HealthStatus
from robotchat.observability.logging import get_logger
from robotchat.providers.base import VadCapabilities, VadEvent
from robotchat.providers.registry import register_provider

logger = get_logger("robotchat.audio.vad")

_DEFAULT_MODEL_PATH = "assets/silero_vad.onnx"


# ---------------------------------------------------------------- 自适应噪声底


class AdaptiveNoiseFloor:
    """滑动窗口低分位能量作为噪声底；语音期间冻结，避免被近端语音抬高。"""

    def __init__(
        self,
        window: int = 60,
        percentile: float = 10.0,
        initial_db: float = -60.0,
        min_samples: int | None = None,
    ) -> None:
        if window < 1:
            raise ValueError(f"window 必须 >= 1：{window}")
        self.percentile = percentile
        self.initial_db = initial_db
        self.min_samples = min_samples if min_samples is not None else max(3, window // 3)
        self._history: deque[float] = deque(maxlen=window)

    def update(self, energy_db: float, allow: bool = True) -> None:
        if allow:
            self._history.append(energy_db)

    @property
    def floor_db(self) -> float:
        if len(self._history) < self.min_samples:
            return self.initial_db
        return float(np.percentile(np.asarray(self._history), self.percentile))

    def __len__(self) -> int:
        return len(self._history)

    def reset(self) -> None:
        self._history.clear()


# ---------------------------------------------------------------- 模型后端


class VadModel(Protocol):
    sample_rate: int
    frame_samples: int

    def reset(self) -> None: ...

    def set_speaking(self, speaking: bool) -> None: ...

    def probability(self, samples: np.ndarray) -> float: ...


class EnergyVadModel:
    """能量 + 自适应噪声底后端：无 ONNX 模型时的诊断 / 单测路径。"""

    sample_rate = 16000
    frame_samples = 320
    span_db = 12.0

    def __init__(self, floor: AdaptiveNoiseFloor | None = None) -> None:
        self._floor = floor or AdaptiveNoiseFloor(window=30)
        self._speaking = False
        self._calibrated = False

    def reset(self) -> None:
        self._floor.reset()
        self._speaking = False
        self._calibrated = False

    def set_speaking(self, speaking: bool) -> None:
        self._speaking = speaking

    def probability(self, samples: np.ndarray) -> float:
        energy_db = rms_dbfs(samples)
        self._floor.update(energy_db, allow=not self._speaking)
        if not self._calibrated:
            # 启动后先用一段静音标定噪声底，避免把现场稳态噪声误判为语音
            if len(self._floor) >= self._floor.min_samples:
                self._calibrated = True
            return 0.0
        margin = energy_db - self._floor.floor_db
        return max(0.0, min(1.0, margin / self.span_db))


class SileroVadModel:
    """Silero VAD ONNX 后端（进程内单实例，模型文件由 assets 提供）。"""

    sample_rate = 16000

    def __init__(self, model_path: str | Path = _DEFAULT_MODEL_PATH) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确指引
            raise RuntimeError(
                "onnxruntime 未安装，无法运行 Silero VAD；请安装 onnxruntime 或改用 energy 后端"
            ) from exc

        path = Path(model_path)
        if not path.exists():
            raise FileNotFoundError(
                f"Silero VAD 模型缺失：{path}；"
                "请将 silero_vad.onnx 放入 assets/（deployment §2 资产清单）"
            )

        self._session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self._inputs = {item.name: item for item in self._session.get_inputs()}
        self._outputs = [item.name for item in self._session.get_outputs()]
        if "state" in self._inputs:
            self._variant = "state"
        elif {"h", "c"} <= set(self._inputs):
            self._variant = "h_c"
        else:
            raise ValueError(
                f"不支持的 Silero VAD 模型签名：{sorted(self._inputs)}（期望 state 或 h/c 版本）"
            )

        shape = self._inputs["input"].shape
        last = shape[-1] if shape and isinstance(shape[-1], int) and shape[-1] > 0 else 512
        self.frame_samples = int(last)
        # v5（state 变体）每步输入 = 上一帧尾部上下文 + 当前帧；
        # 只喂裸帧会把上下文位置错位，模型对语音的概率恒低（实测 0.002）
        self._context_size = 64 if self.sample_rate == 16000 else 32
        self._state: Any = None
        self._context = np.zeros(0, dtype=np.float32)
        self.reset()

    def reset(self) -> None:
        if self._variant == "state":
            self._state = np.zeros((2, 1, 128), dtype=np.float32)
            self._state_name = "state"
            self._context = np.zeros(self._context_size, dtype=np.float32)
        else:
            self._context = np.zeros(0, dtype=np.float32)
            self._state = (
                np.zeros((2, 1, 64), dtype=np.float32),
                np.zeros((2, 1, 64), dtype=np.float32),
            )
            self._state_name = "h"

    def set_speaking(self, speaking: bool) -> None:
        return None

    def probability(self, samples: np.ndarray) -> float:
        audio = np.asarray(samples, dtype=np.float32).ravel()
        if self._context.size:
            audio = np.concatenate((self._context, audio))
        x = audio.reshape(1, -1)
        rate = np.asarray(self.sample_rate, dtype=np.int64)
        if self._variant == "state":
            feeds = {"input": x, "state": self._state, "sr": rate}
        else:
            state_h, state_c = self._state
            feeds = {"input": x, "h": state_h, "c": state_c, "sr": rate}
        outputs = self._session.run(self._outputs, feeds)

        value = float(np.asarray(outputs[0]).ravel()[0])
        if self._variant == "state":
            self._state = outputs[1]
            if self._context.size:
                self._context = x[0, -self._context.size :].copy()
        else:
            self._state = (outputs[1], outputs[2])
        return value


# ---------------------------------------------------------------- VAD 引擎


class HysteresisVad:
    """基于概率的清音/浊音迟滞判定：起音多帧确认 + 尾点静音 + 最大时长保护。"""

    def __init__(self, model: VadModel, settings: VadSettings, name: str) -> None:
        self.name = name
        self.model = model
        self.settings = settings
        self.capabilities = VadCapabilities(
            sample_rate=model.sample_rate, frame_ms=20
        )

        self._pending = np.zeros(0, dtype=np.float32)
        self._events: deque[VadEvent] = deque()
        self._speaking = False
        self._onset_frames = 0
        self._silence_samples = 0
        self._utterance_samples = 0
        self._last_ts = 0.0
        self._processed_frames = 0
        self._errors = 0
        self._speech_starts = 0

        self._silence_samples_limit = int(model.sample_rate * settings.silence_ms / 1000)
        self._max_utterance_samples = int(
            model.sample_rate * settings.max_utterance_ms / 1000
        )

    # ------------------------------------------------------------ 契约

    def process(self, frame: AudioFrame) -> VadEvent | None:
        pcm = pcm16_to_float(frame.data, frame.channels)
        if pcm.ndim > 1:
            pcm = pcm.mean(axis=1)
        if frame.sample_rate != self.model.sample_rate:
            pcm = resample_float(pcm, frame.sample_rate, self.model.sample_rate)

        self._pending = np.concatenate((self._pending, pcm))
        frame_samples = self.model.frame_samples
        while self._pending.size >= frame_samples:
            chunk = self._pending[:frame_samples]
            self._pending = self._pending[frame_samples:]
            self._step(chunk, frame.ts_device)

        self._last_ts = frame.ts_device
        self._processed_frames += 1
        return self._events.popleft() if self._events else None

    def reset(self) -> None:
        self._pending = np.zeros(0, dtype=np.float32)
        self._events.clear()
        self._speaking = False
        self._onset_frames = 0
        self._silence_samples = 0
        self._utterance_samples = 0
        self.model.reset()

    async def health(self) -> HealthStatus:
        if self._errors:
            return HealthStatus.degraded(
                f"{self.name} 推理失败 {self._errors} 次；已处理 {self._processed_frames} 帧"
            )
        return HealthStatus.ok(
            f"{self.name} frames={self._processed_frames} starts={self._speech_starts}"
        )

    # ------------------------------------------------------------ 内部

    def _step(self, chunk: np.ndarray, ts: float) -> None:
        try:
            probability = float(self.model.probability(chunk))
        except Exception:  # 推理异常不应拖垮采集链路
            self._errors += 1
            logger.exception("vad_inference_failed", vad=self.name)
            return

        energy_db = rms_dbfs(chunk)
        threshold = self.settings.threshold

        if self._speaking:
            self._utterance_samples += chunk.size
            if probability >= threshold:
                self._silence_samples = 0
            else:
                self._silence_samples += chunk.size
                if self._silence_samples >= self._silence_samples_limit:
                    self._finish(ts, energy_db, reason="silence")
                    return
            if (
                self._max_utterance_samples > 0
                and self._utterance_samples >= self._max_utterance_samples
            ):
                self._finish(ts, energy_db, reason="max_utterance")
            return

        if probability >= threshold:
            self._onset_frames += 1
            if self._onset_frames >= self.settings.start_frames:
                self._speaking = True
                self._speech_starts += 1
                self._silence_samples = 0
                # 起音确认前的帧也计入本句时长
                self._utterance_samples = self._onset_frames * chunk.size
                self.model.set_speaking(True)
                self._events.append(VadEvent("speech_start", energy_db=energy_db, ts=ts))
                logger.debug(
                    "vad_speech_start", vad=self.name, energy_db=round(energy_db, 2), ts=ts
                )
        else:
            self._onset_frames = 0
            self.model.set_speaking(False)

    def _finish(self, ts: float, energy_db: float, reason: str) -> None:
        self._speaking = False
        self.model.set_speaking(False)
        self._onset_frames = 0
        self._silence_samples = 0
        self._utterance_samples = 0
        self._events.append(VadEvent("speech_end", energy_db=energy_db, ts=ts))
        logger.debug("vad_speech_end", vad=self.name, reason=reason, ts=ts)


def _settings_from(config: dict[str, Any]) -> VadSettings:
    allowed = set(VadSettings.model_fields)
    return VadSettings(**{key: value for key, value in config.items() if key in allowed})


@register_provider("vad", "silero_vad")
class SileroVad(HysteresisVad):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        settings = _settings_from(cfg)
        model_path = cfg.get("model_path") or _DEFAULT_MODEL_PATH
        super().__init__(SileroVadModel(model_path), settings, name="silero_vad")


@register_provider("vad", "energy_vad")
class EnergyVad(HysteresisVad):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        super().__init__(EnergyVadModel(), _settings_from(cfg), name="energy_vad")
