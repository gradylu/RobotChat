# 唤醒词模型目录（T-3A.2）

运行链路（onnxruntime 直连，不依赖 openwakeword 包）：

```
16 kHz PCM → melspectrogram.onnx → embedding_model.onnx → 唤醒词模型 → score
```

## 已入库资产（Apache-2.0，来源 openWakeWord v0.5.1 发布包）

| 文件 | 作用 |
| --- | --- |
| `melspectrogram.onnx` | 固定参数 mel 前端（32 维，10 ms 帧移） |
| `embedding_model.onnx` | Google speech_embedding 主干（输出 96 维） |
| `hey_jarvis_v0.1.onnx` | 英文预训练唤醒词（「hey jarvis」） |

配置见 `config/providers.yaml` 的 `wakeword.drivers.openwakeword` 中
`melspec_model_path` / `embedding_model_path` 与 `model_paths`。

## 中文唤醒词（待补）

`wake_robot_v1.onnx`（「你好机器人」）**没有公开预训练资产**，需要用合成语音自训
（无需人工录音）。训练流水线与配置在 `tools/wakeword/`：

```powershell
# 一次性：合成样本 + 训练 + 导出 + 阈值标定（自动创建独立 .venv-train）
.\tools\wakeword\run_train.ps1 -Stage all

# 分步 / 复验
.\tools\wakeword\run_train.ps1 -Stage prepare
.\tools\wakeword\run_train.ps1 -Stage train
.\tools\wakeword\run_train.ps1 -Stage verify -Wav var\wakeword_train\positives\pos_0000.wav
```

- 配置：`tools/wakeword/train_zh_wakeword.yaml`
  中修改唤醒词文本、TTS 后端、样本量、训练超参与阈值目标；
- 产物直接写到 `assets/wakeword/wake_robot_v1.onnx`（与 `providers.yaml` 的 `model_paths` 对齐），
  训练结束会打印建议阈值与测试集召回 / 误报率；
- `torch` 只装在独立 `.venv-train`，不进应用镜像（design §1.1 白名单）。

在缺失状态下：英文唤醒仍可用，自检第 13 项返回 **WARN**（部分语言不可用）；
若所有模型或前端模型都缺失则返回 **FAIL**。

## 替换 / 扩展

- 换词只需替换 ONNX 文件与 `model_paths`，不改代码（FR-14.2）；
- `wake_phrases` 与 `model_paths` 顺序对齐，用于命中后的播报文案；
- 模型文件随镜像 `COPY assets/ ./assets/` 打包（deployment §2）。
