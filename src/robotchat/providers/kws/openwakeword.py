"""openWakeWord ONNX 唤醒词驱动（T-3A.2、FR-14.1 / FR-14.2 / FR-14.17）。

- 纯本地推理：用 onnxruntime 直连三段式 ONNX（见 `oww_onnx.py`），音频不出本机（CON-6）；
- 多模型并行：`model_paths` 可配置多份（中英双语 / 方言变体），逐帧输出 score；
- 推理函数可注入，单测无需真实模型与 onnxruntime；
- 防误唤醒：置信度阈值、命中冷却、可选二次确认（FR-14.6）；多词同时命中取最高分，
  同分取配置顺序第一个；同一窗口只触发一次（FR-14.17）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import numpy as np

from robotchat.audio.buffer import pcm16_to_float, resample_float
from robotchat.audio.frame import AudioFrame
from robotchat.health import HealthStatus
from robotchat.observability.logging import get_logger
from robotchat.providers.base import KwsCapabilities, KwsStream, WakeEvent
from robotchat.providers.registry import register_provider

logger = get_logger("robotchat.providers.kws.openwakeword")

InferenceFn = Callable[[np.ndarray], dict[str, float]]

DEFAULT_THRESHOLD = 0.6
DEFAULT_COOLDOWN_MS = 2000
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_FRAME_MS = 80


@register_provider("kws", "openwakeword")
class OpenWakeWordKws:
    """openWakeWord 驱动：逐帧推理 + 多模型并行 + 阈值 / 冷却 / 二次确认。"""

    name = "openwakeword"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        inference: InferenceFn | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        cfg = dict(config or {})
        self.model_paths = [str(item) for item in cfg.get("model_paths") or []]
        self.melspec_model_path = str(
            cfg.get("melspec_model_path") or "assets/wakeword/melspectrogram.onnx"
        )
        self.embedding_model_path = str(
            cfg.get("embedding_model_path") or "assets/wakeword/embedding_model.onnx"
        )
        self.wake_phrases = [str(item) for item in cfg.get("wake_phrases") or []]
        self.threshold = float(cfg.get("threshold", DEFAULT_THRESHOLD))
        self.cooldown_ms = float(cfg.get("cooldown_ms", DEFAULT_COOLDOWN_MS))
        self.confirm_twice = bool(cfg.get("confirm_twice", False))
        self.sample_rate = int(cfg.get("sample_rate", DEFAULT_SAMPLE_RATE))
        self.frame_ms = int(cfg.get("frame_ms", DEFAULT_FRAME_MS))
        self._clock = clock

        self._inference = inference
        self._model: Any | None = None
        self._ready = False
        self._load_error = ""
        self._queue: asyncio.Queue[WakeEvent | None] | None = None
        self._stream: OpenWakeWordStream | None = None
        self._last_hit_at = float("-inf")
        self._streak_key = ""
        self._streak_count = 0

        self.frames_processed = 0
        self.detections = 0
        self.last_scores: dict[str, float] = {}
        self.peak_scores: dict[str, float] = {}

        multi = len(self.model_paths) > 1 or inference is not None
        self.capabilities = KwsCapabilities(
            multi_wakeword=multi,
            custom_phrase=True,
            sample_rate=self.sample_rate,
            frame_ms=self.frame_ms,
        )

    # ------------------------------------------------------------ 生命周期

    async def warmup(self) -> None:
        """加载模型到内存；模型文件缺失时抛出明确错误（由注册表标记不可用）。"""
        if self._ready or self._inference is not None:
            self._ready = True
            return
        await asyncio.to_thread(self._load_models)

    def _load_models(self) -> None:
        if not self.model_paths:
            raise RuntimeError("wakeword.model_paths 未配置")
        required = [self.melspec_model_path, self.embedding_model_path, *self.model_paths]
        missing = [path for path in required if not Path(path).exists()]
        if missing:
            raise FileNotFoundError("唤醒词模型缺失：" + ", ".join(missing))
        # 仅用 onnxruntime 复刻三段式前端，避免引入 openwakeword 包及其 scipy 依赖
        from robotchat.providers.kws.oww_onnx import OpenWakeWordOnnx

        backend = OpenWakeWordOnnx(
            self.model_paths,
            self.melspec_model_path,
            self.embedding_model_path,
        )
        self._model = backend
        self._inference = backend
        self._ready = True

    async def open_stream(self, cancel: Any = None) -> KwsStream:
        del cancel
        if self._inference is None:
            await self.warmup()
        if self._stream is not None:
            await self._stream.close()
        self._queue = asyncio.Queue()
        self._stream = OpenWakeWordStream(self)
        logger.info(
            "kws_stream_opened",
            models=len(self.model_paths),
            threshold=self.threshold,
            confirm_twice=self.confirm_twice,
        )
        return self._stream

    def events(self) -> AsyncIterator[WakeEvent]:
        async def generator() -> AsyncIterator[WakeEvent]:
            while True:
                queue = self._queue
                if queue is None:
                    return
                event = await queue.get()
                if event is None:
                    return
                yield event

        return generator()

    async def health(self) -> HealthStatus:
        if self._ready:
            return HealthStatus.ok(f"已加载 {len(self.model_paths)} 个唤醒词模型")
        if self._inference is not None:
            return HealthStatus.ok("推理函数已注入")
        missing = [
            path
            for path in (self.melspec_model_path, self.embedding_model_path, *self.model_paths)
            if not Path(path).exists()
        ]
        if missing:
            return HealthStatus.unavailable("唤醒词模型缺失：" + ", ".join(missing))
        return HealthStatus.degraded("模型存在但尚未加载")

    async def aclose(self) -> None:
        if self._stream is not None:
            await self._stream.close()
        self._model = None
        self._ready = False

    # ------------------------------------------------------------ 逐帧检测

    def _process_frame(self, pcm: np.ndarray, ts: float) -> WakeEvent | None:
        self.frames_processed += 1
        scores = self._run_inference(pcm)
        self.last_scores = dict(scores)
        # 峰值记录便于现场调阈值（只升不降）
        for key, score in scores.items():
            value = float(score)
            if value > self.peak_scores.get(key, float("-inf")):
                self.peak_scores[key] = value
        candidates = [
            (key, float(score))
            for key, score in scores.items()
            if float(score) >= self.threshold
        ]
        if not candidates:
            self._streak_key = ""
            self._streak_count = 0
            return None

        # 多词同时命中：取最高置信度；同分取配置顺序第一个（FR-14.17）
        best_key, best_score = candidates[0]
        for key, score in candidates[1:]:
            if score > best_score:
                best_key, best_score = key, score

        if best_key != self._streak_key:
            self._streak_key = best_key
            self._streak_count = 1
        else:
            self._streak_count += 1
        if self.confirm_twice and self._streak_count < 2:
            return None

        now = self._clock()
        if now - self._last_hit_at < self.cooldown_ms / 1000.0:
            return None
        self._last_hit_at = now
        self.detections += 1
        phrase = self._phrase_for(best_key)
        logger.info(
            "wake_detected",
            phrase=phrase,
            confidence=round(best_score, 4),
            model=best_key,
        )
        return WakeEvent(type="detected", phrase=phrase, confidence=best_score, ts=ts)

    def _run_inference(self, pcm: np.ndarray) -> dict[str, float]:
        if self._inference is None:
            self._load_error = "模型尚未加载"
            return {}
        try:
            return dict(self._inference(pcm))
        except Exception as exc:
            self._load_error = repr(exc)
            logger.exception("kws_inference_failed")
            return {}

    def _phrase_for(self, key: str) -> str:
        if key in self.wake_phrases:
            return key
        stems = [Path(path).stem for path in self.model_paths]
        if key in stems:
            index = stems.index(key)
            if index < len(self.wake_phrases):
                return self.wake_phrases[index]
        return key

    def _emit(self, event: WakeEvent) -> None:
        if self._queue is not None:
            self._queue.put_nowait(event)

    def snapshot(self) -> dict[str, Any]:
        return {
            "driver": self.name,
            "ready": self._ready,
            "models": list(self.model_paths),
            "phrases": list(self.wake_phrases),
            "threshold": self.threshold,
            "cooldown_ms": self.cooldown_ms,
            "confirm_twice": self.confirm_twice,
            "frames": self.frames_processed,
            "detections": self.detections,
            "last_scores": {
                key: round(value, 4) for key, value in self.last_scores.items()
            },
            "peak_scores": {
                key: round(value, 4) for key, value in self.peak_scores.items()
            },
            "load_error": self._load_error,
        }


class OpenWakeWordStream:
    """把采集帧对齐到 KWS 帧长后逐帧推理（帧长由 capabilities 声明）。"""

    def __init__(self, provider: OpenWakeWordKws) -> None:
        self._provider = provider
        self._sample_rate = provider.sample_rate
        self._frame_samples = max(1, int(provider.sample_rate * provider.frame_ms / 1000))
        self._buffer = np.zeros(0, dtype=np.float32)
        self._closed = False
        self.frames_fed = 0

    async def feed(self, frame: AudioFrame) -> None:
        if self._closed:
            return
        pcm = pcm16_to_float(frame.data, frame.channels)
        if pcm.ndim > 1:
            pcm = pcm.mean(axis=1)
        if frame.sample_rate != self._sample_rate:
            pcm = resample_float(pcm, frame.sample_rate, self._sample_rate)
        self._buffer = np.concatenate((self._buffer, np.asarray(pcm, dtype=np.float32)))
        while self._buffer.size >= self._frame_samples:
            block = self._buffer[: self._frame_samples]
            self._buffer = self._buffer[self._frame_samples :]
            self.frames_fed += 1
            event = self._provider._process_frame(block, time.time())
            if event is not None:
                self._provider._emit(event)

    async def close(self) -> None:
        self._closed = True
        queue = self._provider._queue
        if queue is not None:
            queue.put_nowait(None)
        if self._provider._stream is self:
            self._provider._stream = None
            self._provider._queue = None


__all__ = ["OpenWakeWordKws", "OpenWakeWordStream"]
