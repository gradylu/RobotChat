"""openWakeWord 三段式 ONNX 推理后端（T-3A.2）。

链路：16 kHz PCM → melspectrogram.onnx → embedding_model.onnx → 唤醒词模型 → score。

刻意不依赖 `openwakeword` Python 包：该包会引入 scipy / scikit-learn，与 design §1.1
依赖白名单冲突；这里只用项目已允许的 onnxruntime 复刻其流式特征管线，行为与
openWakeWord v0.5.1 的 `AudioFeatures` + `Model.predict` 对齐（见部署文档 §2）。
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

import numpy as np
import onnxruntime as ort

MEL_BINS = 32
MEL_FRAMES = 76
MEL_MAX_FRAMES = 10 * 97
EMBEDDING_SIZE = 96
FEATURE_MAX_FRAMES = 120
CHUNK_SAMPLES = 1280  # 80 ms @ 16 kHz
CONTEXT_SAMPLES = 160 * 3  # 补足 25 ms 窗与帧边界
RAW_MAX_SAMPLES = 16000 * 10


class OpenWakeWordOnnx:
    """把任意长度的语音块累积为 80 ms 分块并输出每个唤醒词模型的分数。"""

    def __init__(
        self,
        model_paths: list[str],
        melspec_model_path: str,
        embedding_model_path: str,
        *,
        ncpu: int = 1,
    ) -> None:
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = max(1, ncpu)
        self._melspec = ort.InferenceSession(
            str(melspec_model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._embedding = ort.InferenceSession(
            str(embedding_model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._models = {
            Path(path).stem: ort.InferenceSession(
                str(path), sess_options=options, providers=["CPUExecutionProvider"]
            )
            for path in model_paths
        }
        # 模型输入帧数由 ONNX 形状声明（openWakeWord 常用 16 帧）
        self._input_frames: dict[str, int] = {}
        for name, session in self._models.items():
            shape = session.get_inputs()[0].shape
            self._input_frames[name] = int(shape[1]) if len(shape) > 1 else 16

        self._melspec_buffer = np.ones((MEL_FRAMES, MEL_BINS), dtype=np.float32)
        self._feature_buffer = np.zeros((FEATURE_MAX_FRAMES, EMBEDDING_SIZE), dtype=np.float32)
        self._raw: deque[int] = deque(maxlen=RAW_MAX_SAMPLES)
        self._pending = np.empty(0, dtype=np.int16)
        self._last_scores: dict[str, float] = {name: 0.0 for name in self._models}

    @property
    def model_names(self) -> list[str]:
        return list(self._models)

    def __call__(self, pcm: np.ndarray) -> dict[str, float]:
        """输入 float32 PCM（[-1, 1]），返回最近一个 80 ms 分块的唤醒词分数。"""
        samples = np.clip(np.asarray(pcm, dtype=np.float32), -1.0, 1.0)
        int16 = (samples * 32767.0).astype(np.int16)
        pending = np.concatenate((self._pending, int16)) if self._pending.size else int16
        scores = dict(self._last_scores)
        while pending.size >= CHUNK_SAMPLES:
            block = pending[:CHUNK_SAMPLES]
            pending = pending[CHUNK_SAMPLES:]
            self._raw.extend(int(value) for value in block)
            scores = self._process_chunk()
        self._pending = pending
        return scores

    def _process_chunk(self) -> dict[str, float]:
        recent = np.asarray(self._recent_raw(), dtype=np.int16)
        melspec = self._melspectrogram(recent)
        self._melspec_buffer = np.vstack((self._melspec_buffer, melspec))
        if self._melspec_buffer.shape[0] > MEL_MAX_FRAMES:
            self._melspec_buffer = self._melspec_buffer[-MEL_MAX_FRAMES:, :]

        window = self._melspec_buffer[-MEL_FRAMES:][None, :, :, None].astype(np.float32)
        embedding = np.asarray(self._embedding.run(None, {"input_1": window})[0]).reshape(-1)
        self._feature_buffer = np.vstack(
            (self._feature_buffer, embedding.reshape(1, EMBEDDING_SIZE).astype(np.float32))
        )
        if self._feature_buffer.shape[0] > FEATURE_MAX_FRAMES:
            self._feature_buffer = self._feature_buffer[-FEATURE_MAX_FRAMES:, :]

        scores: dict[str, float] = {}
        for name, session in self._models.items():
            frames = self._input_frames[name]
            features = self._feature_buffer[-frames:][None, :, :].astype(np.float32)
            output = session.run(None, {session.get_inputs()[0].name: features})[0]
            scores[name] = float(np.asarray(output).reshape(-1)[0])
        self._last_scores = scores
        return scores

    def _recent_raw(self) -> list[int]:
        """取出最近 80 ms 分块及其 480 样本上下文（与 openWakeWord 流式实现一致）。"""
        total = len(self._raw)
        take = min(total, CHUNK_SAMPLES + CONTEXT_SAMPLES)
        return [self._raw[index] for index in range(total - take, total)]

    def _melspectrogram(self, samples: np.ndarray) -> np.ndarray:
        data = np.asarray(samples, dtype=np.float32)[None, :]
        output = self._melspec.run(None, {"input": data})[0]
        spec = np.squeeze(output)
        if spec.ndim != 2:
            spec = spec.reshape(-1, MEL_BINS)
        # 与 openWakeWord 相同的数值变换，贴近 Google speech_embedding 的输入分布
        return (spec / 10.0 + 2.0).astype(np.float32)


__all__ = ["OpenWakeWordOnnx"]
