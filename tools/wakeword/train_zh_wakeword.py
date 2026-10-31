"""中文唤醒词训练流水线（T-3A.2 / FR-14.14）。

产物是一个 ONNX 唤醒词模型，直接放进 `assets/wakeword/`，运行时用
`robotchat.providers.kws.oww_onnx` 三段式前端推理，不需要 `openwakeword` 包。

torch 不在 design §1.1 的依赖白名单内，因此本脚本只在独立训练环境运行
（`tools/wakeword/run_train.ps1` 会创建 `.venv-train`）：

    prepare  合成正 / 负样本 wav（http 走本地 TTS 服务，sapi 走 Windows 内置语音，dir 用真实录音）
    train    抽 embedding 特征 → 训练 16×96 小网络 → 导出 ONNX → 阈值标定 → 运行时复验
    all      prepare + train
    verify   用运行时后端给指定 wav 打分（不需要 torch）

配置见 `tools/wakeword/train_zh_wakeword.yaml`。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import urllib.request
import wave
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "tools" / "wakeword" / "train_zh_wakeword.yaml"

# 训练环境（.venv-train）不安装本包，直接把 src/ 挂到 sys.path 上复用音频工具
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

# 与 oww_onnx.py / openWakeWord v0.5.1 前端保持一致
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
MEL_BINS = 32
MEL_WINDOW = 76
MEL_HOP = 8
EMBED_DIM = 96
CHUNK_SAMPLES = 1280
EMBED_SECONDS = MEL_HOP / 100.0  # 每个 embedding 前进 80 ms


# ---------------------------------------------------------------- 基础工具


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def resolve(value: str | None, default: Path | None = None) -> Path:
    if not value:
        if default is None:
            raise ValueError("配置缺少路径")
        return default
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def read_wav(path: Path) -> np.ndarray:
    """读取 16 kHz 单声道 int16 PCM；其他采样率 / 声道自动转换。"""
    from robotchat.audio.buffer import float_to_pcm16, pcm16_to_float, resample_float

    with wave.open(str(path), "rb") as reader:
        if reader.getsampwidth() != SAMPLE_WIDTH:
            raise ValueError(f"仅支持 16bit WAV：{path}")
        channels = reader.getnchannels()
        rate = reader.getframerate()
        raw = reader.readframes(reader.getnframes())
    data = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1).astype(np.int16)
    if rate != SAMPLE_RATE:
        mono = resample_float(pcm16_to_float(data.tobytes(), 1), rate, SAMPLE_RATE)
        data = np.frombuffer(float_to_pcm16(mono), dtype=np.int16)
    return data


def write_wav(path: Path, pcm: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(SAMPLE_WIDTH)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(np.asarray(pcm, dtype=np.int16).tobytes())


def apply_gain(pcm: np.ndarray, gain_db: float) -> np.ndarray:
    scaled = pcm.astype(np.float32) * (10.0 ** (gain_db / 20.0))
    return np.clip(scaled, -32768, 32767).astype(np.int16)


def speech_bounds(pcm: np.ndarray, floor_db: float = -45.0) -> tuple[float, float]:
    """用能量门限估计词开始 / 结束时间，抵消 TTS 首尾静音。"""
    frame = int(SAMPLE_RATE * 0.01)
    usable = pcm[: pcm.size - pcm.size % frame].astype(np.float32)
    if usable.size == 0:
        return 0.0, pcm.size / SAMPLE_RATE
    energy = np.abs(usable.reshape(-1, frame)).mean(axis=1)
    threshold = np.max(energy) * (10.0 ** (floor_db / 20.0))
    active = np.flatnonzero(energy >= threshold)
    if active.size == 0:
        return 0.0, pcm.size / SAMPLE_RATE
    return active[0] * 0.01, (active[-1] + 1) * 0.01


def mix_noise(speech: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    if noise.size < speech.size:
        repeats = int(np.ceil(speech.size / max(1, noise.size)))
        noise = np.tile(noise, repeats)
    noise = noise[: speech.size].astype(np.float32)
    speech_f = speech.astype(np.float32)
    rms_speech = float(np.sqrt(np.mean(speech_f**2))) or 1.0
    rms_noise = float(np.sqrt(np.mean(noise**2))) or 1.0
    scale = rms_speech / (rms_noise * (10.0 ** (snr_db / 20.0)))
    return np.clip(speech_f + noise * scale, -32768, 32767).astype(np.int16)


# ---------------------------------------------------------------- 样本合成


def synth_http(text: str, cfg: dict) -> np.ndarray:
    """调用本地 TTS（与 qwen3_tts_local 相同的 /audio/speech 协议，返回 PCM16）。"""
    base = str(cfg.get("base_url", "")).rstrip("/")
    if not base:
        raise ValueError("synth.http.base_url 未配置")
    payload = json.dumps(
        {
            "text": text,
            "voice": cfg.get("voice", "default"),
            "sample_rate": SAMPLE_RATE,
            "format": "pcm",
            "lang": cfg.get("lang", "zh"),
        }
    ).encode()
    request = urllib.request.Request(
        base + str(cfg.get("path", "/audio/speech")),
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=float(cfg.get("timeout_s", 30))) as response:
        raw = response.read()
    if len(raw) % SAMPLE_WIDTH:
        raise ValueError("TTS 返回的 PCM 长度不是 16bit 对齐")
    return np.frombuffer(raw, dtype=np.int16).copy()


def synth_sapi(text: str, cfg: dict, scratch: Path) -> np.ndarray:
    """Windows 内置 SAPI 合成（离线；音色单一，仅用于链路自检 / 小样本兜底）。"""
    if os.name != "nt":
        raise RuntimeError("synth.backend=sapi 仅支持 Windows；请改用 http 或 dir")
    voice = str(cfg.get("voice", "") or "").replace("'", "''")
    select = f"$s.SelectVoice('{voice}');" if voice else ""
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"{select}"
        "$f=New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,"
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
        "[System.Speech.AudioFormat.AudioChannel]::Mono);"
        f"$s.SetOutputToWaveFile('{scratch}', $f);"
        f"$s.Speak('{text.replace(chr(39), chr(39) * 2)}');$s.Dispose()"
    )
    scratch.parent.mkdir(parents=True, exist_ok=True)
    if scratch.exists():
        scratch.unlink()
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
    )
    return read_wav(scratch)


def synthesize(text: str, synth_cfg: dict, scratch: Path) -> np.ndarray:
    backend = str(synth_cfg.get("backend", "http"))
    if backend == "http":
        return synth_http(text, synth_cfg.get("http", {}))
    if backend == "sapi":
        return synth_sapi(text, synth_cfg.get("sapi", {}), scratch)
    raise ValueError(f"未知 synth.backend：{backend}")


def copy_dir_wavs(source: Path, target: Path, prefix: str, start: int = 0) -> int:
    if not source.exists():
        raise FileNotFoundError(f"样本目录不存在：{source}")
    count = 0
    for path in sorted(source.glob("*.wav")):
        write_wav(target / f"{prefix}_{start + count:04d}.wav", read_wav(path))
        count += 1
    return count


def load_noise_bank(noise_dir: Path | None) -> list[np.ndarray]:
    if noise_dir is None or not noise_dir.exists():
        return []
    return [read_wav(path) for path in sorted(noise_dir.glob("*.wav"))]


def cmd_prepare(cfg: dict, work_dir: Path) -> int:
    synth_cfg = dict(cfg.get("synth", {}))
    backend = str(synth_cfg.get("backend", "http"))
    pos_cfg = dict(synth_cfg.get("positives", {}))
    neg_cfg = dict(synth_cfg.get("negatives", {}))
    positives = work_dir / "positives"
    negatives = work_dir / "negatives"
    for folder in (positives, negatives):
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True, exist_ok=True)

    variants = [str(item) for item in pos_cfg.get("variants") or [cfg["model"]["phrase"]]]
    gains = [float(item) for item in pos_cfg.get("gain_db", [0.0])]
    repeats = max(1, int(pos_cfg.get("repeats", 1)))
    index = 0
    if backend == "dir":
        index = copy_dir_wavs(resolve(pos_cfg.get("source_dir")), positives, "pos")
    else:
        for variant in variants:
            for repeat in range(repeats):
                pcm = synthesize(variant, synth_cfg, work_dir / "_scratch.wav")
                write_wav(
                    positives / f"pos_{index:04d}.wav",
                    apply_gain(pcm, gains[repeat % len(gains)]),
                )
                index += 1
    print(f"正样本：{index} 条 → {positives}")

    texts = [str(item) for item in neg_cfg.get("texts") or []]
    neg_repeats = max(1, int(neg_cfg.get("repeats", 1)))
    noise_bank = load_noise_bank(
        resolve(neg_cfg["background_noise_dir"]) if neg_cfg.get("background_noise_dir") else None
    )
    mix_prob = float(neg_cfg.get("noise_mix_prob", 0.0))
    snr_low, snr_high = (float(item) for item in neg_cfg.get("noise_snr_db", [5, 20]))
    rng = random.Random(int(cfg.get("dataset", {}).get("seed", 2026)))
    count = 0
    for text in texts:
        for _repeat in range(neg_repeats):
            pcm = synthesize(text, synth_cfg, work_dir / "_scratch.wav")
            if noise_bank and rng.random() < mix_prob:
                noise = rng.choice(noise_bank)
                start = rng.randrange(0, max(1, noise.size - SAMPLE_RATE))
                pcm = mix_noise(pcm, noise[start:], rng.uniform(snr_low, snr_high))
            write_wav(negatives / f"neg_{count:04d}.wav", apply_gain(pcm, rng.uniform(-3, 3)))
            count += 1
    if backend == "dir":
        count += copy_dir_wavs(resolve(synth_cfg.get("negative_source_dir")), negatives, "negreal")
    if neg_cfg.get("negative_wav_dir"):
        count += copy_dir_wavs(resolve(neg_cfg["negative_wav_dir"]), negatives, "negreal", count)
    noise_clips = int(neg_cfg.get("noise_clips", 0))
    if noise_bank:
        for item in range(noise_clips):
            noise = rng.choice(noise_bank)
            start = rng.randrange(0, max(1, noise.size - SAMPLE_RATE * 2))
            write_wav(
                negatives / f"neg_noise_{item:04d}.wav", noise[start : start + SAMPLE_RATE * 2]
            )
            count += 1
    print(f"负样本：{count} 条 → {negatives}")
    scratch = work_dir / "_scratch.wav"
    if scratch.exists():
        scratch.unlink()
    return 0


# ---------------------------------------------------------------- 前端特征


class Frontend:
    """melspectrogram + embedding 两段 ONNX 前端，与运行时完全一致。"""

    def __init__(self, melspec_path: Path, embedding_path: Path) -> None:
        import onnxruntime as ort

        for path in (melspec_path, embedding_path):
            if not path.exists():
                raise FileNotFoundError(f"前端模型缺失：{path}")
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self._melspec = ort.InferenceSession(
            str(melspec_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._embedding = ort.InferenceSession(
            str(embedding_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._mel_input = self._melspec.get_inputs()[0].name
        self._emb_input = self._embedding.get_inputs()[0].name

    def embeddings(self, pcm: np.ndarray) -> np.ndarray:
        mel = self._melspec.run(
            None, {self._mel_input: np.asarray(pcm, dtype=np.float32)[None, :]}
        )[0]
        mel = np.squeeze(mel).reshape(-1, MEL_BINS)
        mel = mel / 10.0 + 2.0
        # 与 openWakeWord 流式实现一致：前面补 76 帧全 1
        mel = np.vstack([np.ones((MEL_WINDOW, MEL_BINS), dtype=np.float32), mel])
        windows = np.stack(
            [mel[end - MEL_WINDOW : end] for end in range(MEL_WINDOW, mel.shape[0] + 1, MEL_HOP)]
        ).astype(np.float32)
        embeddings = self._embedding.run(None, {self._emb_input: windows[..., None]})[0]
        return np.asarray(embeddings, dtype=np.float32).reshape(windows.shape[0], EMBED_DIM)


def make_windows(embeddings: np.ndarray, in_frames: int) -> np.ndarray:
    if embeddings.shape[0] < in_frames:
        return np.zeros((0, in_frames, EMBED_DIM), dtype=np.float32)
    return np.stack(
        [
            embeddings[start : start + in_frames]
            for start in range(embeddings.shape[0] - in_frames + 1)
        ]
    ).astype(np.float32)


def pad_positive(
    pcm: np.ndarray, rng: random.Random, pad_cfg: dict
) -> tuple[np.ndarray, float, float]:
    """在词前后补极低电平噪声（模拟真实上下文），返回补齐后的 PCM 与词起止时间。"""
    pre_range = [float(item) for item in pad_cfg.get("pre_s", [0.6, 1.6])]
    post_range = [float(item) for item in pad_cfg.get("post_s", [0.2, 0.6])]
    pre = int(SAMPLE_RATE * rng.uniform(*pre_range))
    post = int(SAMPLE_RATE * rng.uniform(*post_range))
    amplitude = 32767 * 10.0 ** (rng.uniform(-60.0, -50.0) / 20.0)
    generator = np.random.default_rng(rng.randrange(2**31))
    head = np.clip(generator.normal(0.0, amplitude, pre), -8, 8).astype(np.int16)
    tail = np.clip(generator.normal(0.0, amplitude, post), -8, 8).astype(np.int16)
    padded = np.concatenate([head, pcm, tail]).astype(np.int16)
    start, end = speech_bounds(pcm)
    offset = head.size / SAMPLE_RATE
    return padded, start + offset, end + offset


def build_dataset(
    files: list[Path],
    *,
    positive: bool,
    frontend: Frontend,
    in_frames: int,
    drop_warmup: int,
    rng: random.Random,
    pad_cfg: dict,
    tolerance_s: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    features: list[np.ndarray] = []
    labels: list[float] = []
    clips: list[int] = []
    for clip_id, path in enumerate(files):
        pcm = read_wav(path)
        word_start, word_end = 0.0, 0.0
        if positive:
            pcm, word_start, word_end = pad_positive(pcm, rng, pad_cfg)
        embeddings = frontend.embeddings(pcm)
        embeddings = embeddings[drop_warmup:]
        windows = make_windows(embeddings, in_frames)
        if windows.shape[0] == 0:
            continue
        if not positive:
            features.extend(windows)
            labels.extend([0.0] * windows.shape[0])
            clips.extend([clip_id] * windows.shape[0])
            continue
        for index, window in enumerate(windows):
            # 窗口末帧对应的音频时间（含被丢弃的 warmup embedding 偏移）
            end_time = EMBED_SECONDS * (index + drop_warmup + in_frames - 1)
            if end_time < word_start or end_time > word_end + tolerance_s:
                # 词前上下文 / 词尾之后的拖尾静音，都应是低分
                label = 0.0
            elif word_end <= end_time <= word_end + tolerance_s:
                label = 1.0
            else:
                # 窗口只覆盖了词的一部分，标签不可靠，丢弃
                continue
            features.append(window)
            labels.append(label)
            clips.append(clip_id)
    if not features:
        return (
            np.zeros((0, in_frames, EMBED_DIM), dtype=np.float32),
            np.zeros(0, dtype=np.float32),
            np.zeros(0, dtype=np.int32),
        )
    return (
        np.stack(features).astype(np.float32),
        np.asarray(labels, dtype=np.float32),
        np.asarray(clips, dtype=np.int32),
    )


def clip_peaks(scores: np.ndarray, labels: np.ndarray, clips: np.ndarray) -> list[float]:
    """每条正样本 clip 的最高分（运行时按流取峰值触发，标定要对齐这一点）。"""
    positive_clips = np.unique(clips[labels > 0.5])
    return [float(np.max(scores[clips == clip])) for clip in positive_clips]


def stack_parts(
    parts: list[tuple[np.ndarray, np.ndarray, np.ndarray]], in_frames: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    items = [item for item in parts if item[0].shape[0]]
    if not items:
        return (
            np.zeros((0, in_frames, EMBED_DIM), dtype=np.float32),
            np.zeros(0, dtype=np.float32),
            np.zeros(0, dtype=np.int32),
        )
    return (
        np.concatenate([item[0] for item in items]),
        np.concatenate([item[1] for item in items]),
        np.concatenate([item[2] for item in items]),
    )


def split_files(
    files: list[Path], split_cfg: dict, rng: random.Random
) -> dict[str, list[Path]]:
    ratio = float(split_cfg.get("train", 0.7))
    val_ratio = float(split_cfg.get("val", 0.15))
    shuffled = list(files)
    rng.shuffle(shuffled)
    train_end = max(1, int(len(shuffled) * ratio))
    val_end = max(train_end, int(len(shuffled) * (ratio + val_ratio)))
    return {
        "train": shuffled[:train_end],
        "val": shuffled[train_end:val_end],
        "test": shuffled[val_end:],
    }


# ---------------------------------------------------------------- 训练 / 导出


def _torch():
    try:
        import torch  # type: ignore[import-not-found]  # 仅训练环境安装
    except ImportError as exc:  # pragma: no cover - 依赖独立训练环境
        raise SystemExit(
            "未找到 torch：训练需独立环境，请先执行 "
            "python -m pip install -r tools/wakeword/requirements-train.txt"
        ) from exc
    return torch


class WakeHead:
    """16×96 embedding → 128 → 128 → 1 的小网络（结构参考 openWakeWord）。"""

    @staticmethod
    def build(torch, in_frames: int, hidden: int):
        return torch.nn.Sequential(
            torch.nn.Flatten(),
            torch.nn.Linear(in_frames * EMBED_DIM, hidden),
            torch.nn.ReLU(),
            torch.nn.Linear(hidden, hidden),
            torch.nn.ReLU(),
            torch.nn.Linear(hidden, 1),
            torch.nn.Sigmoid(),
        )


def choose_threshold(
    positive_peaks: list[float],
    negative_scores: np.ndarray,
    target_recall: float,
    max_fpr: float,
) -> tuple[float, dict[str, float]]:
    """在满足召回 / 误报约束的前提下取最高阈值；无解时退化为召回优先。"""
    candidates: list[tuple[float, float, float]] = []
    for threshold in np.linspace(0.05, 0.95, 91):
        recall = (
            float(np.mean([score >= threshold for score in positive_peaks]))
            if positive_peaks
            else 0.0
        )
        fpr = float(np.mean(negative_scores >= threshold)) if negative_scores.size else 0.0
        candidates.append((float(threshold), recall, fpr))
    feasible = [item for item in candidates if item[1] >= target_recall and item[2] <= max_fpr]
    chosen = feasible[-1] if feasible else max(candidates, key=lambda c: (c[1], -c[2], c[0]))
    return round(chosen[0], 3), {"recall": round(chosen[1], 4), "fpr": round(chosen[2], 4)}


def score_runtime(
    model_path: Path, melspec_path: Path, embedding_path: Path, wavs: list[Path]
) -> dict[str, float]:
    from robotchat.providers.kws.oww_onnx import OpenWakeWordOnnx

    backend = OpenWakeWordOnnx([str(model_path)], str(melspec_path), str(embedding_path))
    key = model_path.stem
    peaks: dict[str, float] = {}
    for path in wavs:
        pcm = read_wav(path).astype(np.float32) / 32768.0
        peak = 0.0
        for start in range(0, max(0, pcm.size - CHUNK_SAMPLES + 1), CHUNK_SAMPLES):
            peak = max(peak, float(backend(pcm[start : start + CHUNK_SAMPLES]).get(key, 0.0)))
        peaks[path.name] = round(peak, 4)
    return peaks


def cmd_train(cfg: dict, work_dir: Path) -> int:
    torch = _torch()
    dataset_cfg = dict(cfg.get("dataset", {}))
    train_cfg = dict(cfg.get("train", {}))
    eval_cfg = dict(cfg.get("evaluate", {}))
    model_cfg = dict(cfg["model"])
    in_frames = int(model_cfg.get("in_frames", 16))
    seed = int(train_cfg.get("seed") or dataset_cfg.get("seed") or 2026)
    torch.manual_seed(seed)
    np.random.seed(seed)
    rng = random.Random(seed)

    positives = sorted((work_dir / "positives").glob("*.wav"))
    negatives = sorted((work_dir / "negatives").glob("*.wav"))
    if not positives or not negatives:
        print("样本缺失：请先执行 prepare", file=sys.stderr)
        return 2

    frontend = Frontend(
        resolve(cfg["frontend"]["melspec_model_path"]),
        resolve(cfg["frontend"]["embedding_model_path"]),
    )
    split_cfg = dict(dataset_cfg.get("split", {}))
    pos_split = split_files(positives, split_cfg, rng)
    neg_split = split_files(negatives, split_cfg, rng)
    pad_cfg = dict(dataset_cfg.get("pad", {}))
    tolerance = float(pad_cfg.get("label_tolerance_s", 0.3))
    drop_warmup = int(dataset_cfg.get("drop_warmup_embeddings", 0))

    def build(files: list[Path], positive: bool) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return build_dataset(
            files,
            positive=positive,
            frontend=frontend,
            in_frames=in_frames,
            drop_warmup=drop_warmup,
            rng=rng,
            pad_cfg=pad_cfg,
            tolerance_s=tolerance,
        )

    def assemble(
        positive_split: list[Path], negative_split: list[Path]
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        x_pos, y_pos, c_pos = build(positive_split, True)
        x_neg, y_neg, c_neg = build(negative_split, False)
        if c_pos.size:
            c_neg = c_neg + int(c_pos.max()) + 1
        return stack_parts([(x_pos, y_pos, c_pos), (x_neg, y_neg, c_neg)], in_frames)

    x_train, y_train, _c_train = assemble(pos_split["train"], neg_split["train"])
    x_val, y_val, c_val = assemble(pos_split["val"], neg_split["val"])
    x_test, y_test, c_test = assemble(pos_split["test"], neg_split["test"])
    if x_train.shape[0] == 0:
        print("训练窗口为空：样本过短或 drop_warmup_embeddings 过大", file=sys.stderr)
        return 2
    print(
        f"窗口数 train={x_train.shape[0]} val={x_val.shape[0]} test={x_test.shape[0]}"
        f"（正样本占比 train={float(np.mean(y_train)):.2%}）"
    )

    device = torch.device(str(train_cfg.get("device", "cpu")))
    model = WakeHead.build(torch, in_frames, int(train_cfg.get("hidden", 128))).to(device)
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=float(train_cfg.get("lr", 1e-3)),
        weight_decay=float(train_cfg.get("weight_decay", 1e-4)),
    )
    pos_weight = float(train_cfg.get("pos_weight", 1.0))
    batch_size = int(train_cfg.get("batch_size", 64))
    patience = int(train_cfg.get("patience", 10))
    epochs = int(train_cfg.get("epochs", 60))
    best_loss = float("inf")
    best_state = {key: value.detach().clone() for key, value in model.state_dict().items()}
    stalled = 0

    def tensors(x: np.ndarray, y: np.ndarray):
        return (
            torch.from_numpy(x).to(device),
            torch.from_numpy(y).to(device).unsqueeze(1),
        )

    x_val_t, y_val_t = tensors(x_val, y_val) if x_val.shape[0] else (None, None)
    for epoch in range(1, epochs + 1):
        model.train()
        order = torch.randperm(x_train.shape[0])
        total = 0.0
        for start in range(0, x_train.shape[0], batch_size):
            index = order[start : start + batch_size]
            batch_x = torch.from_numpy(x_train[index.numpy()]).to(device)
            batch_y = torch.from_numpy(y_train[index.numpy()]).to(device).unsqueeze(1)
            # 训练期增益扰动（±4 dB），提升现场电平差异鲁棒性
            gain = 10 ** ((torch.rand(batch_x.shape[0], 1, 1, device=device) * 8 - 4) / 20)
            prediction = model(batch_x * gain)
            weight = (batch_y > 0.5).float() * (pos_weight - 1.0) + 1.0
            loss = torch.nn.functional.binary_cross_entropy(prediction, batch_y, weight=weight)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total += float(loss.item()) * batch_x.shape[0]
        train_loss = total / x_train.shape[0]
        if x_val_t is not None:
            model.eval()
            with torch.no_grad():
                val_loss = float(
                    torch.nn.functional.binary_cross_entropy(model(x_val_t), y_val_t).item()
                )
        else:
            val_loss = train_loss
        if val_loss < best_loss:
            best_loss = val_loss
            best_state = {key: value.detach().clone() for key, value in model.state_dict().items()}
            stalled = 0
        else:
            stalled += 1
        if epoch % 5 == 0 or epoch == 1:
            print(f"epoch {epoch:3d} train_loss={train_loss:.4f} val_loss={val_loss:.4f}")
        if stalled >= patience:
            print(f"早停：验证损失连续 {patience} 轮未改善")
            break

    model.load_state_dict(best_state)
    model.eval()
    output = resolve(model_cfg.get("output"))
    output.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(
        model,
        torch.zeros(1, in_frames, EMBED_DIM, device=device),
        str(output),
        input_names=["input"],
        output_names=["output"],
        opset_version=int(train_cfg.get("opset", 13)),
        do_constant_folding=True,
        # TorchScript 导出器：不依赖 onnxscript，且各 torch 版本行为一致
        dynamo=False,
    )
    print(f"已导出 ONNX：{output}")

    # 阈值标定用验证集，指标用测试集
    with torch.no_grad():
        def scores(x: np.ndarray) -> np.ndarray:
            if x.shape[0] == 0:
                return np.zeros(0, dtype=np.float32)
            return model(torch.from_numpy(x).to(device)).cpu().numpy().reshape(-1)

        val_scores = scores(x_val)
        test_scores = scores(x_test)

    target_recall = float(eval_cfg.get("target_recall", 0.9))
    max_fpr = float(eval_cfg.get("max_false_positive_rate", 0.01))
    threshold, threshold_stats = choose_threshold(
        clip_peaks(val_scores, y_val, c_val),
        val_scores[y_val <= 0.5],
        target_recall,
        max_fpr,
    )
    test_peaks = clip_peaks(test_scores, y_test, c_test)
    test_recall = float(np.mean([peak >= threshold for peak in test_peaks])) if test_peaks else 0.0
    test_negative = test_scores[y_test <= 0.5]
    metrics = {
        "model": str(output),
        "threshold": threshold,
        "samples": {"positives": len(positives), "negatives": len(negatives)},
        "windows": {
            "train": int(x_train.shape[0]),
            "val": int(x_val.shape[0]),
            "test": int(x_test.shape[0]),
        },
        "val": {
            "recall": round(threshold_stats["recall"], 4),
            "fpr": round(threshold_stats["fpr"], 4),
        },
        "test": {
            "recall": round(test_recall, 4),
            "fpr": round(
                float(np.mean(test_negative >= threshold)) if test_negative.size else 0.0, 4
            ),
            "negative_p95": round(
                float(np.percentile(test_negative, 95)) if test_negative.size else 0.0, 4
            ),
        },
        "train_loss": round(best_loss, 4),
    }
    if threshold_stats["recall"] < target_recall:
        print(
            f"警告：验证集召回 {threshold_stats['recall']:.2f} 未达目标 {target_recall:.2f}，"
            "建议增加正样本变体 / 轮数，或下调 target_recall",
            file=sys.stderr,
        )

    check_clips = int(eval_cfg.get("runtime_check_clips", 3))
    if check_clips > 0 and pos_split["test"]:
        peaks = score_runtime(
            output,
            resolve(cfg["frontend"]["melspec_model_path"]),
            resolve(cfg["frontend"]["embedding_model_path"]),
            pos_split["test"][:check_clips],
        )
        metrics["runtime_check"] = peaks
        lowest = min(peaks.values()) if peaks else 0.0
        metrics["runtime_check_pass"] = lowest >= threshold
        print(f"运行时复验（{len(peaks)} 条测试正样本）：{peaks}，阈值 {threshold}")
        if lowest < threshold:
            print(
                "警告：运行时峰值低于建议阈值，请增加样本 / 轮数，或人工复核 threshold",
                file=sys.stderr,
            )

    (work_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(
        "下一步：确认 config/providers.yaml 的 model_paths 已包含该文件（顺序对应 wake_phrases），"
        "并执行 robotchat doctor 复检第 13 项"
    )
    return 0


def cmd_verify(cfg: dict, wavs: list[Path]) -> int:
    model_path = resolve(cfg["model"]["output"])
    if not model_path.exists():
        print(f"模型不存在：{model_path}（先执行 train）", file=sys.stderr)
        return 2
    if not wavs:
        print("未提供 --wav，跳过", file=sys.stderr)
        return 0
    peaks = score_runtime(
        model_path,
        resolve(cfg["frontend"]["melspec_model_path"]),
        resolve(cfg["frontend"]["embedding_model_path"]),
        wavs,
    )
    metrics_path = (
        resolve(cfg.get("dataset", {}).get("work_dir", "var/wakeword_train")) / "metrics.json"
    )
    threshold = 0.6
    if metrics_path.exists():
        data = json.loads(metrics_path.read_text(encoding="utf-8"))
        threshold = float(data.get("threshold", 0.6))
    for name, peak in peaks.items():
        print(f"{name}: peak={peak} {'OK' if peak >= threshold else 'LOW'}（阈值 {threshold}）")
    return 0


# ---------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=str(DEFAULT_CONFIG), help="训练配置 yaml")
    parser = argparse.ArgumentParser(
        description="中文唤醒词训练流水线（T-3A.2）", parents=[common]
    )
    sub = parser.add_subparsers(dest="stage", required=True)
    sub.add_parser("prepare", parents=[common], help="合成正 / 负样本 wav")
    sub.add_parser("train", parents=[common], help="训练并导出 ONNX + 阈值标定")
    sub.add_parser("all", parents=[common], help="prepare + train")
    verify = sub.add_parser("verify", parents=[common], help="用运行时后端给 wav 打分")
    verify.add_argument("--wav", action="append", default=[], help="待打分 wav，可重复")
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config))
    work_dir = resolve(cfg.get("dataset", {}).get("work_dir", "var/wakeword_train"))
    if args.stage in ("prepare", "all"):
        code = cmd_prepare(cfg, work_dir)
        if code:
            return code
    if args.stage in ("train", "all"):
        return cmd_train(cfg, work_dir)
    if args.stage == "verify":
        return cmd_verify(cfg, [Path(item) for item in args.wav])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
