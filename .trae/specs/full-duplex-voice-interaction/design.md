# RobotChat 全双工语音交互系统 — 技术设计

- 文档版本：v2.1
- 日期：2026-11-01
- 状态：待评审
- 变更（v2.1）：新增意图识别与路由设计（R14、FR-17、NFR-1、A12），见 §9.6；同步架构、数据流、接口、配置、数据模型、ADR 与风险
- 定位：本文档回答「**怎么做**」。需求见 [requirements.md](./requirements.md)，部署运维见 [deployment.md](./deployment.md)，验收见 [checklist.md](./checklist.md)

---

## 1. 技术选型

| 项 | 选型 | 说明 |
| --- | --- | --- |
| 后端语言 | Python 3.10 ~ 3.12 | 双发行版（Humble=3.10 / Jazzy=3.12，CON-10）；代码须在两者上均可运行，避免与 rclpy 的 ABI 冲突（见 ADR-03、ADR-15） |
| 后端框架 | FastAPI + uvicorn | async 原生，WebSocket 支持好，轻量 |
| 并发模型 | asyncio 单事件循环 + 少量线程池（音频解码 / 阻塞 IO） | 避免多进程状态同步开销 |
| 前端 | Vue3 + TypeScript + Vite | 组件化，构建产物为静态文件，由后端托管 |
| 实时通道 | WebSocket（事件流）+ REST（控制与配置） | 协议版本化 |
| 配置 | YAML + 环境变量覆盖 + 启动时 schema 校验 | 配置与镜像分离（FR-10.9） |
| 记忆存储 | SQLite（默认）/ Redis（可选） | 单机零依赖优先 |
| VAD | Silero VAD（ONNX）+ onnxruntime（CPU） | 不引入 PyTorch，满足轻量化约束（ADR-05） |
| AEC | 硬件优先（Profile 声明）；无硬件时用 NumPy 实现 NLMS 软件方案；`audio.aec.mode` 默认 `auto` 自动选择 | 不引入 scipy / speex 重依赖；A4 确认 AEC 能力因机型而异，必须可配置而非写死（ADR-06、CON-13、§6.2） |
| 唤醒词 KWS | 默认 openWakeWord（ONNX）+ onnxruntime（CPU），可插拔 | 本地常开、轻量、无商业 License 依赖（ADR-12） |
| 多语言 / 方言 | 语言能力声明 + 语言路由；本地保 `zh` / `en`，方言走外置或云端方言 ASR | 方言模型体积大，不进镜像（CON-8、ADR-13） |
| 意图识别与路由 | 本地规则快速路径 + 可插拔语义分类驱动（LLM 结构化输出 / 轻量分类器） | 规则路径 ≤ 20ms 串行前置；语义分类与 LLM / RAG 并行，超时或低置信度兜底 `chat`，不成为首响阻塞项（FR-17、ADR-18） |
| 音频设备适配 | `AudioDeviceAdapter` 抽象 + 每机型 Profile（配置化）+ 机型适配器实现 | 各机型 ROS2 采集 / 播放接口不同（A1/A2），差异全部收敛到适配层（CON-11、ADR-14） |
| 容器基础镜像 | `ros:humble-ros-base` / `ros:jazzy-ros-base`（双发行版矩阵） / `python:3.11-slim` + 桥接（备选） | 按 `ROS_DISTRO` 选择标签；见 §3.4、ADR-02、ADR-15 |
| 编排 | Docker Compose | 单机多容器编排，轻量 |
| 进程守护 | systemd 管理 compose 启停 | 开机自启与故障重启 |

### 1.1 依赖分层（轻量化关键设计）

应用运行时依赖被严格分层，重依赖一律不进应用镜像：

| 层 | 依赖 | 是否默认安装 |
| --- | --- | --- |
| core | `fastapi` `uvicorn[standard]` `pydantic` `pyyaml` `numpy` `httpx` `websockets` `onnxruntime` `structlog` | ✅ 默认 |
| ros2 | `rclpy`（由基础镜像或 apt 提供） | ✅ 主方案默认 |
| local-vad | Silero VAD ONNX 模型文件（~2 MB） | ✅ 默认 |
| local-kws | 唤醒词模型文件（openWakeWord ONNX，~2~5 MB） | ✅ 默认 |
| cloud | `dashscope` / `openai` SDK | ⬜ extras `[cloud]`，按需 |
| 算力分层（A5 已确认） | 本体：ASR / TTS 模型服务（独立容器或宿主机进程）+ VAD / KWS 进程内轻量 ONNX + AEC 进程内 NumPy；边端或云端：LLM 与知识库 | 应用镜像只做编排与两类 ONNX 推理（CON-1 / CON-12）；默认本体不跑 LLM（拓扑 2 显式开启除外） |
| 禁止 | `torch` `transformers` `funasr` `vllm` 等重型推理框架 | ❌ 应用镜像内禁止 |

> 核心思想：**应用镜像只做编排、协议适配与 VAD / KWS 两类轻量 ONNX 推理，不做重模型推理**。ASR / TTS 作为独立服务运行（详见 [deployment.md](./deployment.md) §4），这同时满足了「轻量化」与「可插拔」两个约束。
> 算力分层（A5）：语音侧模型（ASR / TTS / VAD / AEC / KWS）位于机器人本体，**LLM 与知识库默认不在本体**（边端或云端，拓扑 2 可显式开启），因此本体资源预算默认只需覆盖语音模型（CON-12）。

---

## 2. 架构总览

```
┌────────────────────────────────────────────────────────────────────┐
│  接入层   WebUI (Vue3 静态资源)   ⇄   REST / WebSocket 事件网关       │
├────────────────────────────────────────────────────────────────────┤
│  编排层   会话编排 Orchestrator                                       │
│           ├─ 状态机 StateMachine（声明式转移表 + Guard + 超时兜底）    │
│           ├─ Turn 管理（turn_id / 陈旧事件丢弃）                       │
│           ├─ 语言路由 LanguageRouter（lang 判定 + 能力协商 + 降级）     │
│           ├─ 意图路由 IntentRouter（规则快速路径 + 语义分类并行）        │
│           ├─ 取消令牌 CancellationToken（父子传播）                    │
│           └─ 看门狗 Watchdog（状态超时 + 全局心跳）                     │
├────────────────────────────────────────────────────────────────────┤
│  能力层   Provider 注册表（可插拔驱动 + 熔断降级 + 能力协商）           │
│           ├─ ASR : FunASR 本地 / 云端流式                            │
│           ├─ LLM : vLLM / Ollama / OpenAI 兼容云端                    │
│           ├─ Intent : 规则 / LLM 结构化输出 / 轻量分类器（可插拔）        │
│           └─ TTS : Qwen3-TTS 本地 / 云端                              │
├────────────────────────────────────────────────────────────────────┤
│  音频层   AudioTransport 抽象（容器边界内，通过 DDS 或桥接触达设备）    │
│           ├─ 设备适配层 AudioDeviceAdapter（机型 Profile 决定话题/服务绑定）│
│           ├─ 采集链路：ROS2 采集 → 抖动缓冲 → AEC → VAD(会话期常开)     │
│           ├─ 待机链路：ROS2 采集 → 唤醒词检测 KWS(常开, 低功耗)          │
│           ├─ 播放链路：播放队列 → ROS2 播放 → 扬声器                   │
│           └─ Barge-in 检测器（能量 + 时长 + 保护窗口 + 语义确认）       │
├────────────────────────────────────────────────────────────────────┤
│  数据层   会话记忆（SQLite / Redis）· 配置中心 · 事件日志 · 指标         │
├────────────────────────────────────────────────────────────────────┤
│  执行层   工具注册表 → 执行适配器（ROS2；REST / MQTT 预留）→ 机器人本体   │
└────────────────────────────────────────────────────────────────────┘
              ▲                                      ▲
              │ DDS 域（宿主网络）                     │ HTTP / WebSocket
   宿主机 ROS2 节点（麦克风阵列 / 扬声器 / 控制服务）   本体语音模型（ASR/TTS）；LLM/KB 在边端/云端
```

---

## 3. 部署形态对架构的约束（容器化设计要点）

### 3.1 关键约束回顾

- CON-1 轻量化：应用镜像不得内置推理框架 → 强制「模型服务外置」架构
- CON-2 Docker + ROS2 共存：须与宿主机 ROS2 在同一 DDS 域协同，且不破坏既有功能

### 3.2 模型服务外置带来的架构影响

由于模型服务（本体语音侧的 FunASR / Qwen3-TTS，以及边端 / 云端的 LLM 与知识库）必须作为**外部服务**接入，Provider 层在架构上被明确要求：

1. 所有 provider 驱动只依赖 **HTTP / WebSocket** 协议与外部服务通信，不做进程内推理。
2. provider 驱动必须实现**健康检查**与**连接复用**（长连接池），因为跨进程调用的握手开销会直接体现在首响延迟上。
3. 本地驱动与云端驱动在协议层面保持对称（建议统一为 OpenAI 兼容协议），使得「本地」与「云端」的切换只需改 `base_url` 与模型名，而非换驱动实现。
4. 应用启动时须对 provider 做**预热**（warm-up）请求，避免首个用户轮次承担模型冷启动耗时。

### 3.3 音频链路跨越容器边界

音频采集与播放需要跨越容器边界访问宿主机 ROS2 的 DDS 域，设计上要求：

1. `AudioTransport` 抽象必须独立于具体传输实现，使「容器内直连 DDS」与「宿主机桥接节点」两种方案可以互换（见 ADR-02）。
2. 音频数据在容器边界上传输时须为**原始 PCM**，不做重复编解码，以控制延迟。
3. 采集与播放须使用**独立连接/会话**，避免相互阻塞。
4. 机型差异（话题 / 服务名、消息类型、采样率、停播方式）由 §3.6 的设备适配层吸收，`AudioTransport` 只负责「如何触达 DDS」，两层不得混淆。

### 3.4 ROS2 接入方案与发行版矩阵

**发行版矩阵（CON-10、ADR-15）**：机器人现场主要为 Humble 与 Jazzy，两者都须支持。同一份源码在两种发行版上构建、运行，差异只允许出现在基础镜像与依赖来源，不允许分叉业务代码。

| 发行版 | 基础镜像 | Ubuntu | Python | 镜像标签 | 说明 |
| --- | --- | --- | --- | --- | --- |
| **Humble** | `ros:humble-ros-base` | 22.04 | 3.10 | `robotchat:<version>-humble` | 现存机型主力版本 |
| **Jazzy** | `ros:jazzy-ros-base` | 24.04 | 3.12 | `robotchat:<version>-jazzy` | 新机型 / 升级版本 |
| 备选（解耦） | `python:3.11-slim` + 桥接节点 | — | 3.11 | `robotchat:<version>-slim` | 发行版与应用依赖冲突时的兜底 |

**选路规则**：部署脚本读取宿主机 `ROS_DISTRO`（或 `deploy/.env` 显式指定），拉取对应标签镜像；镜像内启动时校验 `ROS_DISTRO` 与自身构建版本一致，不一致直接 FAIL（防止 Humble 版镜像跑在 Jazzy 环境）。

| 方案 | 描述 | 优点 | 缺点 | 适用条件 |
| --- | --- | --- | --- | --- |
| **方案 A（主方案）容器内直连 DDS** | 应用镜像基于 `ros:<distro>-ros-base`（按上表选标签），容器内直接 `import rclpy`；`network_mode: host` + 挂载 `/dev/shm` + 统一 `ROS_DOMAIN_ID` / `RMW_IMPLEMENTATION` | 延迟最低，无额外组件，部署最简 | Python 版本被 ROS2 发行版锁定；镜像体积较大（~1.2 GB） | 现场 ROS2 为 Humble 或 Jazzy，且应用不强依赖特定 Python 版本 |
| **方案 B（备选）宿主机桥接节点** | 应用镜像保持 `python:3.x-slim`（体积 ~300 MB）；宿主机运行一个轻量 `robotchat_ros2_bridge` 节点（rclpy，按宿主机发行版构建），将音频与控制指令通过本地 WebSocket / UDP 转发给容器 | 应用镜像轻量、Python 版本自由 | 多一个组件与一跳转发，延迟略增；需额外维护桥接进程（且需按发行版各构建一次） | 应用需固定 Python 版本，或不愿绑定 ROS2 镜像，或 ROS2 发行版与应用依赖冲突 |

**决策规则**：默认采用**方案 A** 并维护 Humble / Jazzy 两个标签；当出现下列任一情况时切换方案 B：
- 机器人 ROS2 环境不便作为镜像基础（如已深度定制、无法打镜像）；
- 需要应用与 ROS2 解耦以便独立升级；
- 存在第三个发行版且短期内无法纳入矩阵，需先用桥接过渡。

无论采用哪种方案，`AudioTransport`、`AudioDeviceAdapter` 与 `ToolAdapter` 的接口不变，切换仅需更换驱动配置（`audio.transport: ros2_native | ros2_bridge`）。

### 3.5 网络边界与暴露面

| 端口 / 通道 | 用途 | 暴露范围 |
| --- | --- | --- |
| DDS（动态端口） | 与宿主机 ROS2 通信 | 仅本机（host 网络内） |
| WebUI（HTTP） | 控制台访问 | 局域网，需鉴权 |
| 健康检查端口 | 编排与运维探测 | 本机 |
| 指标端口（可选） | 监控采集 | 本机 / 内网监控 |

### 3.6 机器人音频设备适配层（A1/A2 落地设计）

**问题**：各机型的 ROS2 麦克风采集接口（话题名、消息类型、采样率、声道、时间戳语义）与扬声器播报方式（service / topic / 流式写入 / 音量控制）都不相同。若在业务代码里直接写 topic 名，每接一个机型就要改一遍编排、VAD、KWS、打断逻辑，必然返工（CON-11）。

**分层**：`AudioTransport`（容器如何触达 DDS，§3.4）之下再分出一层 `AudioDeviceAdapter`（机型如何说话），两层正交：

```
业务层（VAD / KWS / ASR / 播放队列 / Barge-in）
        │  只用 AudioFrame / AudioChunk 语义接口
        ▼
AudioDeviceAdapter（机型无关的抽象，两个契约）
   ├─ AudioCaptureContract : open() / stream() -> AsyncIterator[AudioFrame] / close()
   ├─ AudioPlaybackContract: open() / play(chunk) / flush() / set_volume() / close()
        │  由 Profile 决定具体绑定
        ▼
Adapter 实现（每机型一份，可复用基类）
        ▼
AudioTransport（ros2_native / ros2_bridge / file_io）
        ▼
rclpy / DDS / 文件
```

#### 3.6.1 契约定义

```python
class AudioCaptureContract(Protocol):
    async def open(self) -> None: ...
    def stream(self) -> AsyncIterator[AudioFrame]: ...   # 统一为 16kHz/16bit/单声道，或 Profile 声明后由适配层转换
    def last_timestamp(self) -> float: ...               # 单调递增，供回声剔除与打断判定
    async def close(self) -> None: ...

class AudioPlaybackContract(Protocol):
    async def open(self) -> None: ...
    async def play(self, chunk: AudioChunk) -> None: ...  # 背压有界，阻塞即代表设备侧已接受
    async def flush(self) -> None: ...                    # 立即静音，Barge-in 预算 300ms 内
    async def set_volume(self, level: float) -> None: ...
    async def close(self) -> None: ...
```

- 上层**不得**出现 `create_subscription("...")` / `create_client("...")` 之类的具体调用；这些只允许出现在适配器实现内。
- `AudioFrame` 除 PCM 外必须带 `ts_device`（设备时间戳）与 `seq`（序号），供丢帧检测与回声对齐。

#### 3.6.2 机型 Profile（配置化，不硬编码）

> 首批需交付的 5 份 Profile（A11 / FR-16.12）：`agibot_x2_pro`（智元灵犀 X2 Pro 探索版）、`agibot_x2_ultra`（智元灵犀 X2 Ultra 旗舰版）、`agibot_a2`（智元 A2）、`agibot_a3`（智元 A3）、`unitree_g1`（宇树 G1）。下方结构为模板，各机型字段按 D12 采集结果填写。

```yaml
# config/devices/<model>.yaml
profile: acr-robot-v2            # Profile 名，由 audio.device_profile 选择
display_name: "ACR 机器人 V2"
ros_distro: [humble, jazzy]      # 支持的发行版；与镜像标签不匹配时启动 FAIL

capabilities:
  capture: true
  playback: true
  echo_reference: true           # 能否提供「即将播放的音频」作为 AEC 参考（FR-1.5）
  hardware_aec: false
  volume_control: true
  playback_flush: stream_stop    # stream_stop | service | topic_msg | volume_to_zero

capture:
  kind: topic
  topic: "/mic/audio_raw"
  msg_type: "audio_common_msgs/msg/AudioData"   # 或自定义消息，字段映射如下
  fields: { data: "data" }
  sample_rate: 16000
  channels: 1
  bit_depth: 16
  frame_ms: 20
  qos: { reliability: best_effort, depth: 10 }
  timestamp: { source: header_stamp, unit: ns, monotonic: true }

playback:
  kind: topic                    # topic | service | action
  topic: "/spk/audio_out"
  msg_type: "audio_common_msgs/msg/AudioData"
  sample_rate: 24000             # 与内部 16k 不同 → 适配层负责重采样
  channels: 1
  bit_depth: 16
  flush:
    kind: service
    service: "/spk/stop"
    msg_type: "std_srvs/srv/Trigger"

volume:
  kind: topic
  topic: "/spk/volume"
  msg_type: "std_msgs/msg/Float32"
  range: [0.0, 1.0]
```

要点：
- **消息字段映射**（`fields`）解决「同名不同结构」问题；未知消息类型时适配器报明确错误，不做猜测。
- **采样率 / 声道 / 位深**由 Profile 声明，与内部管线不一致时在适配器内转换；转换须用有界缓冲，不得引入秒级延迟。
- **flush 方式**由 Profile 声明；`flush()` 的语义统一为「调用返回后设备侧不再输出残留音频」，不同机型的实现方式不同但契约一致。

#### 3.6.3 注册与选路

| 步骤 | 行为 |
| --- | --- |
| 1 启动加载 | 读取 `audio.device_profile`；为空时尝试按 `robot_model` 自动匹配；探测失败时若配置了 `audio.fallback_profile` 则使用并 WARN，否则**拒绝启动**（FR-16.9、FR-16.11） |
| 2 Profile 校验 | schema 校验（必填项、能力与实现一致性、`ros_distro` 与镜像匹配） |
| 3 适配器解析 | `device_adapters` 注册表中按 `kind` + 机型名解析实现；未注册的实现直接报错 |
| 4 自检 | 校验话题 / 服务实际存在且可收发（见 deployment §7.3 第 16 项） |
| 5 运行时 | 上层只持有 `AudioCaptureContract` / `AudioPlaybackContract` 引用 |

#### 3.6.4 时序与时钟

- 采集时间戳统一换算为**单调递增的秒**（`ts_device`），用于回声剔除与打断判定的时间对齐；设备时间戳不可靠时退化为到达时间，但须在 Profile 中显式声明（`timestamp.source: arrival`）。
- 播放侧维护「已提交但未播放完」的时长估算，作为 AEC 参考信号的延迟补偿量；不同机型的设备缓冲深度由 Profile 的 `playback.latency_ms` 声明。
- 采样率转换后须保持 `sample_rate * frame_ms` 帧对齐，避免 VAD / KWS 帧边界漂移。

#### 3.6.5 契约测试（新机型准入）

统一的 `tests/contract/audio/` 套件，任何机型适配器实现都须通过：

| 用例 | 判定 |
| --- | --- |
| 采集帧率 | 连续 30s 实际帧数与理论值偏差 ≤ 1%，无空洞 |
| 采集时间戳 | 单调递增，无回退；丢帧可被 `seq` 检出并告警 |
| 播放出声 | 播放测试音频，设备侧确认有声（人工或回采） |
| `flush()` 时延 | 调用到静音 ≤ 100ms（Barge-in 300ms 预算内留足余量） |
| 采样率转换 | 24k→16k / 双声道→单声道 波形与谱特征正确，无爆音 |
| 断线重连 | 拔掉 / 重启设备侧节点后 ≤ 5s 自动重连并恢复数据流 |
| 能力一致性 | Profile 声明 `hardware_aec=true` 但实测无回声参考时判定失败 |

#### 3.6.6 换机型不改代码的边界

| 允许改 | 禁止改 |
| --- | --- |
| 新增 `config/devices/<model>.yaml` | 状态机、编排器 |
| 新增 `audio/adapters/<model>.py`（或复用通用适配器） | VAD / ASR / LLM / TTS / KWS provider |
| 在 `device_adapters` 注册表登记新实现 | 打断判定阈值与逻辑 |
| 补充契约测试用例 | WebUI 协议与数据模型 |

### 3.7 算力部署边界（A5 落地设计）

**结论先行**：语音链路必须在本体闭环（采集 → AEC → VAD → KWS → ASR → TTS → 播放），LLM 与知识库放到边端或云端。理由：语音侧对延迟与断网可用性最敏感，而 LLM 单轮可容忍几十毫秒到数百毫秒的网络开销，且 7B 级模型在本体上与语音模型抢资源会直接破坏打断与首响指标。

| 组件 | 部署位置 | 形态 | 断网时 |
| --- | --- | --- | --- |
| 麦克风采集 / 扬声器播放 | 本体（ROS2） | 适配层接入 | 不受影响 |
| AEC / VAD / KWS | 本体（应用进程内） | ONNX + NumPy，轻量 | 不受影响 |
| ASR | 本体 | 独立容器 / 宿主机进程（FunASR 等） | 不受影响 |
| TTS | 本体 | 独立容器 / 宿主机进程 | 不受影响 |
| LLM | 边端或云端 | OpenAI 兼容 HTTP / WS | 熔断 → 兜底话术 / 可选本地小模型 |
| 知识库 / RAG | 边端或云端 | HTTP 检索服务 | 跳过增强，正常对话（FR-4.7） |

**配置表现**：三者只是 `base_url` 不同，驱动实现相同，因此「本体 / 边端 / 云端」的迁移不需要改代码（ADR-17）。

```yaml
providers:
  llm:
    active: edge_llm                  # 边端 / 云端；拓扑 2 时指向本机地址（§1.2）
    drivers:
      edge_llm:
        base_url: ${LLM_BASE_URL}     # 边端: http://10.0.0.12:8000/v1  云端: https://api.example.com/v1
        model: ${LLM_MODEL}
        allow_local_llm: false        # true 仅用于拓扑 2（本机 LLM），须通过资源校验（§3.7、T-2.11）
        timeout_ms: { connect: 300, first_token: 800, total: 30000 }
  knowledge:
    active: http_rag                  # http_rag | none
    drivers:
      http_rag:
        base_url: ${KB_BASE_URL}      # 留空 = 关闭知识库增强（等价 none，启动 WARN 不阻断）
        top_k: 4
        max_chars: 1200
        timeout_ms: 200               # 计入首响预算；超时即跳过（FR-4.7）
```

**本体资源不足时的策略**（按优先级，均可配置）：

1. 降低 ASR / TTS 实例的并发与批大小（不回退到云端，保延迟）；
2. 空闲卸载：长时间无交互时释放 ASR / TTS 常驻内存（唤醒后再加载）；关闭唤醒词常开聆听时无唤醒事件，不得卸载 ASR（否则失去唯一输入通道），仅可卸载 TTS（FR-14.8）；
3. 降级到云端 ASR / TTS（仅在公网稳定时）；
4. 极端场景退化为半双工 / 仅文本对话（保命而非保体验）。

> 不允许「静默超卖」：启动自检须报告本体模型服务的实际占用与余量，超预算时 WARN 并给出建议（FR-13.12）。

---

## 4. 全双工数据流

```
麦克风阵列 ──ROS2──▶ 抖动缓冲 ──▶ AEC(回声抑制) ──┬─▶ 唤醒词检测 KWS（待机态常开，低功耗）
                                                   │        │ 唤醒命中
                                                   │        ▼
                                                   │   播放唤醒反馈（应答语 / 提示音）
                                                   │        │
                                                   │        ▼
                                                   │   进入会话活跃期（活跃窗口内无需重复唤醒）
                                                   ▼
                                        VAD(会话活跃期内常开, 永不停止)
                                                          │
                                            ┌─────────────┴──────────────┐
                                            ▼                            ▼
                                     起音/端点检测                  Barge-in 检测器
                                            │                            │
                                            ▼                            │ 打断成立
                                     ASR 流式会话(热流,不销毁)             │
                                       │ partial / final + lang           │
                                       ▼                                  │
                       意图识别与路由 IntentRouter（§9.6）                 │
                       ├─ 规则快速路径 stop / confirm / cancel（≤20ms）     │
                       └─ 语义分类 chat / knowledge / command（并行）       │
                                       │ intent / confidence / slots      │
                         ┌─────────────┴─────────────┐                    │
                         ▼                           ▼                    │
                chat / knowledge                  command                 │
                         │                           │                    │
                         ▼                           ▼                    │
        语言路由 + 记忆检索 + 提示词组装         工具校验 / 二次确认         │
          （knowledge → RAG 注入）                   │                    │
                         ▼                           ▼                    │
                LLM 流式生成                   工具执行适配器（ROS2）        │
                         │                           │                    │
                     tool call                       └─▶ 结果回灌 LLM      │
                         │                           │                    │
                         ▼                           ▼                    │
          按句切分 ──▶ TTS 流式合成 ──▶ 播放队列 ──▶ 扬声器 / ROS2         │
```

**架构核心**：VAD 与 ASR 采集链路**从不因播报而停止**；播放链路独立并发且可被整体冲刷（flush）。这两点是「边听边说 + 秒打断」的物理基础。

**意图路由不新增状态与链路**：规则快速路径在 `LISTENING → THINKING` 之间串行执行（≤20ms），语义分类与 LLM / RAG 并行；`command` 须在首块音频入队前转为工具路径，`stop` 复用 Barge-in 取消路径，低置信度与窗口外的 `confirm` / `cancel` 一律按 `chat` 处理（§9.6）。

---

## 5. 状态机设计

### 5.1 状态定义

| ID | 状态 | 说明 | 超时兜底 |
| --- | --- | --- | --- |
| S0 | `BOOTING` | 启动中：加载配置、注册驱动、provider 健康检查与预热 | 30s → `ERROR` |
| S1 | `IDLE` | **待机态**：唤醒词检测 KWS 常开且低功耗，ASR 会话关闭；等待唤醒词命中或 WebUI 手动激活（FR-11.2） | 常驻无超时（受全局心跳保护，FR-7.2；KWS 故障 → `DEGRADED`） |
| S2 | `LISTENING` | 检测到用户说话，ASR 流式识别中 | 无 final 超 20s → 强制端点 |
| S3 | `THINKING` | 已收到 final，LLM 推理中，尚未出声 | 首 token 超 5s → 降级兜底话术 |
| S4 | `SPEAKING` | TTS 合成 + 播放中，麦克风仍常开可被打断 | 首音频超 3s / 总播报超 120s → 中断 |
| S5 | `INTERRUPTING` | 打断过渡态：停播、取消上游、清队列、丢弃陈旧事件 | 500ms → 强制复位到 `LISTENING` |
| S6 | `EXECUTING` | 工具 / 任务异步执行中，对话管线保持可用 | 按工具定义，默认 30s |
| S7 | `DEGRADED` | 降级模式：某 provider 熔断，使用备用链路或固定话术 | 30s 周期健康探测回切 |
| S8 | `ERROR` | 不可恢复错误，自恢复流程中 | 恢复尝试 3 次后重建会话 |
| S9 | `ACTIVE_IDLE` | **活跃待命**：会话活跃窗口内，VAD 与 ASR 常开，等待用户继续说话（**无需唤醒词**；`wakeword.enabled=false` 时为本模式常驻状态） | 活跃窗口超时（默认 60s）→ `IDLE`；`wakeword.enabled=false` 时不触发 |
| S10 | `WAKING` | **唤醒确认**：播放唤醒反馈音 / 应答语，准备进入聆听 | 5s 内无有效语音 → `IDLE` |

> 全局看门狗（1s 心跳）触发自恢复时固定走 `ERROR → IDLE`（关闭唤醒词时 → `ACTIVE_IDLE`）：取消当前轮次的对话链路（LLM 生成 / TTS 合成 / 播放队列）、保留已持久化记忆、未完成轮次标记 `aborted`，并记录 ERROR 日志与 `watchdog_recovered` 指标、推送 WebUI 告警；已移交执行层的异步任务不被取消，继续执行并按 FR-14.16 播报结果（FR-7.7）。
> 心跳「进展」判据（FR-7.7）：收到音频帧、VAD / KWS 推理产出、事件被处理、provider 健康探测返回，任一发生即视为有进展；`IDLE` 与常开聆听 `ACTIVE_IDLE` 属常驻等待，不得因无对话事件被判无进展（否则会误触发自恢复）。

### 5.2 事件定义

| 分类 | 事件 |
| --- | --- |
| 生命周期 | `EV_START` `EV_READY` `EV_BOOT_FAIL` `EV_SESSION_END` |
| 音频 / VAD | `EV_SPEECH_START` `EV_SPEECH_END` `EV_SILENCE` |
| ASR | `EV_ASR_PARTIAL` `EV_ASR_FINAL` `EV_ASR_ERROR` |
| 唤醒词 | `EV_WAKE_DETECTED` `EV_WAKE_ACK_DONE` `EV_SESSION_ACTIVE_TIMEOUT` |
| LLM | `EV_LLM_FIRST_TOKEN` `EV_LLM_DELTA` `EV_LLM_TOOL_CALL` `EV_LLM_DONE` `EV_LLM_ERROR` |
| TTS / 播放 | `EV_TTS_FIRST_CHUNK` `EV_TTS_DONE` `EV_TTS_ERROR` `EV_PLAYBACK_DONE` `EV_PLAYBACK_ERROR` |
| 打断 | `EV_BARGE_IN` |
| 工具 | `EV_TOOL_DONE` `EV_TOOL_ERROR` `EV_TOOL_TIMEOUT` |
| 控制 | `EV_CANCEL` `EV_RESET` `EV_MUTE` `EV_UNMUTE` `EV_TEXT_INPUT` `EV_SESSION_START` |
| 健康 | `EV_TIMEOUT_<STATE>` `EV_HEALTH_FAIL` `EV_HEALTH_OK` `EV_WATCHDOG_TICK` |

### 5.3 转移表

| 当前状态 | 事件 | 守卫条件 | 目标状态 | 动作 |
| --- | --- | --- | --- | --- |
| BOOTING | EV_READY | 必需 provider 健康且预热完成；唤醒词已加载 | IDLE（唤醒词关闭时直接 `ACTIVE_IDLE`） | 建立会话，通知 WebUI |
| BOOTING | EV_BOOT_FAIL / EV_TIMEOUT | — | ERROR | 记录原因，进入自恢复 |
| **IDLE** | **EV_WAKE_DETECTED** | **置信度达标 + 冷却期已过** | **WAKING** | **记录唤醒指标，播放唤醒反馈** |
| IDLE | EV_TIMEOUT | — | DEGRADED | KWS 无心跳，标记降级并告警 |
| IDLE | EV_SESSION_START | WebUI 手动开始会话（免唤醒） | ACTIVE_IDLE | 视作一次显式唤醒，开启活跃窗口（FR-11.2） |
| IDLE | EV_TEXT_INPUT | 待机态文本输入（免唤醒） | THINKING | 视作显式唤醒，开启活跃窗口，新建 turn_id，跳过 ASR（FR-11.2） |
| WAKING | EV_WAKE_ACK_DONE | — | ACTIVE_IDLE | 开启活跃窗口计时，激活 ASR 会话（可预连接） |
| WAKING | EV_SPEECH_START | 用户在确认阶段直接说话 | LISTENING | 跳过反馈等待，新建 turn_id |
| WAKING | EV_TIMEOUT | 5s 内无有效语音 | IDLE | 关闭 ASR，回到待机 |
| **ACTIVE_IDLE** | **EV_SPEECH_START** | **起音确认通过** | **LISTENING** | **新建 turn_id，启动/复用 ASR 会话** |
| ACTIVE_IDLE | EV_TEXT_INPUT | — | THINKING | 新建 turn_id，跳过 ASR |
| ACTIVE_IDLE | EV_SESSION_ACTIVE_TIMEOUT | 活跃窗口超时（`wakeword.enabled=false` 时不触发） | IDLE | 刷新后仍无交互则关闭 ASR，回到待机 |
| ACTIVE_IDLE | EV_SESSION_END | 用户主动结束 | IDLE | 关闭会话，回到待机 |
| LISTENING | EV_ASR_PARTIAL | turn 匹配 | LISTENING | 推送 partial 到 WebUI |
| LISTENING | EV_SPEECH_START | turn 匹配 | LISTENING | 重置尾点计时（用户继续说） |
| LISTENING | EV_ASR_FINAL | turn 匹配且文本有效 | THINKING | 写入记忆，组装提示词，调用 LLM |
| LISTENING | EV_TIMEOUT | — | ACTIVE_IDLE | 丢弃轮次并告警（仍在活跃期，无需重新唤醒） |
| THINKING | EV_LLM_FIRST_TOKEN | turn 匹配 | SPEAKING | 启动 TTS 流与播放 |
| THINKING | EV_LLM_TOOL_CALL | 工具已授权 | EXECUTING | 异步派发工具任务 |
| THINKING | EV_BARGE_IN | 判定成立 | INTERRUPTING | 取消 LLM，进入打断序列 |
| THINKING | EV_TIMEOUT | — | DEGRADED | 播报兜底话术 |
| SPEAKING | EV_TTS_FIRST_CHUNK | turn 匹配 | SPEAKING | 开始播放，启动保护窗口 |
| **SPEAKING** | **EV_BARGE_IN** | **判定成立** | **INTERRUPTING** | **立即停播 + 取消上游** |
| SPEAKING | EV_TTS_DONE | 队列未空 | SPEAKING | 等待播放完成 |
| SPEAKING | EV_PLAYBACK_DONE | turn 匹配 | ACTIVE_IDLE | 标记轮次完成并持久化，刷新活跃窗口 |
| SPEAKING | EV_TIMEOUT | — | DEGRADED | 中断播报并告警 |
| INTERRUPTING | （内部清理完成） | 清理全部完成 | LISTENING | 复用热 ASR 会话，新建 turn_id |
| INTERRUPTING | EV_TIMEOUT | — | LISTENING | 强制复位并告警（防卡死） |
| EXECUTING | EV_TOOL_DONE | turn 匹配 | THINKING | 结果回灌 LLM 生成播报 |
| EXECUTING | EV_BARGE_IN | 有播报 | INTERRUPTING | 打断播报；工具任务默认继续（可配置取消） |
| EXECUTING | EV_TOOL_TIMEOUT / EV_TOOL_ERROR | — | THINKING | 失败原因回灌 LLM |
| DEGRADED | EV_HEALTH_OK | 主 provider 恢复 | ACTIVE_IDLE | 回切主链路并播报提示，刷新活跃窗口 |
| 任意 | EV_CANCEL | — | INTERRUPTING | 全链路取消（ASR 采集流除外） |
| 任意 | EV_RESET | 运维触发 | BOOTING | 强制重建会话 |
| ERROR | 恢复成功 | — | IDLE | 重建会话并通知 WebUI |

### 5.4 关键不变量

| ID | 不变量 | 保障手段 |
| --- | --- | --- |
| INV-1 | 任意时刻只有一个有效 `turn_id` | Turn 管理器统一签发与校验，非当前 turn 事件静默丢弃并计数 |
| INV-2 | 除常驻等待状态外，任何状态均有超时兜底，禁止无超时等待 | 状态机注册表强制要求每个状态声明 timeout；`IDLE` 与常开聆听 `ACTIVE_IDLE` 显式声明「常驻无超时」并由全局心跳兜底（FR-7.2、FR-7.7），启动时校验 |
| INV-3 | 播报 / 思考 / 执行期间采集与 VAD 必须运行 | 音频采集任务生命周期独立于状态机，不随播报停止 |
| INV-4 | 转移原子、非法事件忽略且告警 | 转移表 + 单写者模型（事件总线串行化）+ 异常隔离 |
| INV-5 | 取消传播至 LLM / TTS / 播放 / 工具 | 层级 CancellationToken，子任务注册到父令牌 |
| INV-6 | 进入 `INTERRUPTING` 必须完成完整清理 | 清理序列为固定步骤，任一步失败记录但不中断后续步骤 |
| INV-7 | 内部队列有界 | 所有队列由有界队列封装，超限按策略丢弃 + 告警计数 |
| INV-8 | **会话活跃期内的打断不得以唤醒词为前提** | Barge-in 检测器不依赖 KWS；`ACTIVE_IDLE` / `SPEAKING` 下 KWS 不参与打断判定 |
| INV-9 | 待机态不持有 ASR 会话；活跃态不运行 KWS | 状态机在 `IDLE` ↔ `ACTIVE_IDLE` 切换时显式启停 ASR 与 KWS 任务，避免资源常驻（关闭唤醒词时按 FR-14.8 常驻 `ACTIVE_IDLE`，属显式配置例外） |
| INV-10 | 意图判定不得成为首响阻塞项 | 规则快速路径串行 ≤ 20ms；语义分类与 LLM / RAG 并行，超时 200ms 按 `chat` 兜底；分类失败只降级不中断对话（FR-17.3、FR-17.8） |
| INV-11 | 低置信度与确认窗口外的 `confirm` / `cancel` 不得触发工具 | `confidence < intent.confidence_threshold` 一律路由 `chat`；`confirm` / `cancel` 仅在高风险工具确认窗口内生效（FR-17.4、FR-17.6） |

### 5.5 状态机实现形态

采用**声明式转移表**而非散落的 if/else：

```python
Transition(
    src=State.SPEAKING,
    event=EventType.BARGE_IN,
    guard=lambda ctx: ctx.barge_in.accepted,
    dst=State.INTERRUPTING,
    action=InterruptAction(),      # 返回可 await 的清理动作
)
```

- 转移表在启动时构建并做**完整性校验**（每个状态是否有超时、是否有出边、是否有兜底）。
- 所有事件经**有界队列串行化**处理，保证状态转移无竞态（单写者）。
- 每次转移写入审计日志：`from` `to` `event` `turn_id` `latency_us` `reason`。

### 5.6 唤醒词与待机唤醒设计

> B-06 已确认：需要唤醒词。本节描述唤醒机制如何嵌入状态机与音频链路，且不破坏全双工体验。

#### 5.6.1 待机 / 活跃两态资源模型

| 状态 | 唤醒词检测 KWS | VAD | ASR 会话 | 说明 |
| --- | --- | --- | --- | --- |
| `IDLE`（待机） | ✅ 常开（低功耗） | 低功耗能量门控 | ❌ 关闭 | 仅在本地跑 KWS，节省算力与云端费用 |
| `WAKING`（唤醒确认） | ❌ 暂停 | ✅ 开启 | 预连接 / 预热 | 播放反馈音，准备接收指令 |
| `ACTIVE_IDLE`（活跃待命） | ❌ 关闭 | ✅ 常开 | ✅ 热流保持 | 活跃窗口内无需重复唤醒 |
| `LISTENING` / `THINKING` / `SPEAKING` / `EXECUTING` | ❌ 关闭 | ✅ 常开 | ✅ 热流保持 | 全双工 + 秒打断 |

关键点：**KWS 只在待机态运行，因此它不会成为打断链路上的任何一环**（满足 FR-14.9、INV-8）。
> 关闭唤醒词（`wakeword.enabled=false`，FR-14.8）时不存在 `IDLE` / `WAKING`：启动即 `ACTIVE_IDLE`，KWS 不运行，VAD 与 ASR 热流常开（资源口径按该模式单独实测），结果播报直接进行且支持 Barge-in（FR-14.16）。

#### 5.6.2 唤醒流程时序

```
T+0ms     KWS 命中（置信度 ≥ 阈值，且距上次命中超过冷却期）
T+~20ms   状态机 IDLE → WAKING；刷新活跃窗口
T+~30ms   播放唤醒反馈（应答语如「我在」或提示音）
T+~300ms  预连接 / 预热 ASR 会话（若 wakeword.preopen_asr = true）
T+~500ms  反馈播放完成 → EV_WAKE_ACK_DONE → ACTIVE_IDLE
T+~500ms+ 用户说话 → VAD 起音 → LISTENING → 正常对话链路
```

#### 5.6.3 会话活跃期

- 唤醒成功后开启活跃窗口（`wakeword.active_window_ms`，默认 60s）。
- 活跃窗口在**每次有效交互**（`EV_ASR_FINAL`、`EV_PLAYBACK_DONE`、工具结果播报）后刷新。
- 窗口到期且无交互 → `EV_SESSION_ACTIVE_TIMEOUT` → `IDLE`，同时关闭 ASR 会话、释放连接。
- 关闭唤醒词（`wakeword.enabled=false`）时不启用活跃窗口超时：始终保持 `ACTIVE_IDLE` 与 ASR 常开，否则释放后无法再唤醒（FR-14.5、FR-14.8）。
- **常开聆听的 ASR 本地性（FR-14.8）**：该模式默认使用本地 ASR 驱动；若显式改用云端 ASR，须置 `wakeword.open_ear.cloud_asr_ack=true`（默认 false）。配置加载器做跨字段校验（`enabled=false` + 云端 ASR 驱动 + 未置 ack → FAIL 阻塞启动），置位后启动自检 WARN 并输出「音频将持续上传至云端」，同时埋点 `open_ear_cloud_audio_ms`（仅时长，不含音频）。
- 窗口内的打断**不需要唤醒词**，与无唤醒词方案行为完全一致（≤ 300ms）。
- **长任务联动（FR-14.15）**：长任务在发起时刷新一次窗口、结果播报完成后再刷新一次，执行期间不刷新；任务时长超过窗口时照常回 `IDLE`（关闭唤醒词时无窗口超时，任务全程在 `ACTIVE_IDLE`），任务在后台继续，不得因回待机取消任务。
- **待机态结果播报（FR-14.16）**：默认缓存结果 + 可选提示音（不拉起 ASR），下次唤醒后优先播报并刷新窗口；`wakeword.standby.announce=true` 时允许待机播报，但播报期间不拉起 ASR、不支持 Barge-in（半双工），播完仍回 `IDLE`。关闭唤醒词（常开聆听）时不存在待机态，结果直接按活跃期规则播报并支持 Barge-in，上述缓存与 `standby.announce` 均不适用（FR-14.8）。

#### 5.6.4 防误唤醒

| 手段 | 说明 | 默认 |
| --- | --- | --- |
| 置信度阈值 | KWS 输出得分低于阈值不响应 | 0.6（按现场标定） |
| 命中冷却 | 一次命中后固定时间内不再响应，避免连续误触发 | 2s |
| 二次确认（可选） | 命中后播报「你是叫我吗？」，等待用户确认 | 关闭 |
| 音素 / 距离约束 | 唤醒词长度 ≥ 3 音节的推荐约束，降低短词误触发 | 配置校验提示 |
| 回声抑制复用 | 播报期间 KWS 不运行，天然规避自唤醒 | 由资源模型保证 |

#### 5.6.5 可插拔与关闭开关

- 唤醒词引擎抽象为 `WakeWordProvider`（见 §7.1），与 ASR / LLM / TTS 同级的驱动注册机制。
- `wakeword.enabled: false` 时：状态机启动即进入 `ACTIVE_IDLE`，KWS 任务不启动，行为退化为常开聆听（FR-14.8）。
- 更换唤醒词只需更换模型文件与配置，不涉及状态机与编排代码（FR-14.2）。

#### 5.6.6 唤醒相关指标

`wake_count`（唤醒次数）`wake_false_positive`（误唤醒次数，由用户在活跃期内「无指令超时」间接统计）`wake_latency_ms`（命中到反馈开始）`active_window_expired`（活跃窗口到期次数）`open_ear_cloud_audio_ms`（常开聆听下云端 ASR 累计上传音频时长，仅时长不含音频，FR-14.8）

---

## 6. Barge-in 详细设计

### 6.1 判定条件（需全部满足）

| # | 条件 | 默认参数 | 可配 |
| --- | --- | --- | --- |
| C1 | 当前处于有播报的状态 | — | — |
| C2 | VAD 判定为语音（能量 ≥ 自适应噪声底 + 动态门限 Δ） | Δ = 播报期 +6 dB | ✅ |
| C3 | 连续语音帧数达标 | ≥ 3 帧 / ~60 ms | ✅ |
| C4 | 已过播放起始保护窗口 | 随 AEC 模式自适应：hardware 100 ms / software 200 ms / none 300 ms | ✅ |
| C5 | 语义确认（可选）：ASR partial 长度 ≥ K 且与当前播报文本相似度 < 阈值 | K = 2 字，相似度 < 0.8 | ✅ |

> C5 用于在无硬件 AEC 的场景下兜底防自打断；有可靠 AEC 时可关闭以换取更快的打断速度。
> 不判定打断的例外（FR-6.1）：`barge_in.mode=disabled` 的半双工兜底；`wakeword.standby.announce=true` 的待机播报（FR-14.16）；高风险工具二次确认播报（FR-9.10，该轮语音作为确认回答处理）。
> 模式选择（A4）：默认 `auto`——有硬件 AEC 用硬件，无则自动启用软件 NLMS；`auto` 的选择结果须在启动日志、`/api/health` 与 WebUI 中可见，便于现场确认到底生效了哪一种。

### 6.2 回声抑制（防自打断）策略组合

| # | 策略 | 说明 | 优先级 |
| --- | --- | --- | --- |
| 1 | 硬件 AEC | 设备 / 麦克风阵列自带（含波束成形）。`mode=auto` 时若 Profile 声明 `hardware_aec: true` 且探测通过则直接启用（`mode=hardware`） | 最高 |
| 2 | 软件 AEC | 将播放音频作为 reference 做 NLMS 自适应滤波后从采集信号减除；`mode=auto` 且无可用硬件 AEC 时**自动启用**（`mode=software`） | 高 |
| 3 | 门限提升 | 播报期间整体抬高 VAD 门限（`barge_in.threshold_boost_db`） | 高 |
| 4 | 保护窗口 | 播放开始后固定时间内不判定打断，规避播放起始瞬态（`barge_in.guard_ms`）；默认值随 AEC 模式自适应：hardware 100ms / software 200ms / none 300ms（FR-6.5） | 高 |
| 5 | 文本回声剔除 | ASR 结果与最近播报文本相似度比对，高相似直接判定为回声丢弃 | 中 |
| 6 | 关闭打断 | 极端噪声环境退化半双工（`barge_in.mode = disabled`） | 兜底 |

### 6.3 打断执行序列（总预算 ≤ 300ms）

```
T+0ms      判定成立 → 状态机原子转移到 INTERRUPTING
T+~10ms    ① 播放队列 flush，扬声器立即静音        ← 优先级最高，先停声
T+~30ms    ② 递增 turn_id（后续陈旧事件自动失效）
T+~40ms    ③ 取消令牌触发：中止 LLM 生成流、中止 TTS 合成流
T+~60ms    ④ 清空 TTS 音频缓冲与待播放队列
T+~80ms    ⑤ 当前轮次标记 interrupted，已播报文本片段写入记忆
T+~120ms   ⑥ 推送 barge_in 事件与各阶段耗时到 WebUI
T+~150ms   ⑦ 状态 → LISTENING，复用热 ASR 会话，直接进入识别
```

执行顺序保证「**先停声、再清理、后恢复识别**」。任一步失败须记录并兜底，但不得阻塞后续步骤。

### 6.4 打断事件记录字段

`timestamp` `session_id` `turn_id` `from_state` `stage_latency`（停播 / 取消 / 清队列 / 恢复识别各阶段耗时）`trigger`（vad / energy / semantic / manual）`speech_energy_db` `noise_floor_db` `echo_suppressed`（是否发生回声抑制）`result`（accepted / rejected）

---

## 7. 可插拔 Provider 设计

### 7.1 抽象接口

```python
class ASRProvider(Protocol):
    name: str
    capabilities: ASRCapabilities          # 流式 / 热词 / 时间戳 / 采样率 / 支持语言集合

    async def open_stream(self, session_id: str, cancel: CancellationToken) -> "ASRStream": ...
    async def health(self) -> HealthStatus: ...
    async def warmup(self) -> None: ...     # 预热，规避冷启动延迟

class ASRStream(Protocol):
    async def feed(self, frame: AudioFrame) -> None: ...
    def events(self) -> AsyncIterator[ASREvent]: ...   # partial / final / error
    async def close(self) -> None: ...

class LLMProvider(Protocol):
    name: str
    capabilities: LLMCapabilities          # 流式 / tool call / 上下文窗口

    async def chat_stream(self, req: ChatRequest, cancel: CancellationToken) -> AsyncIterator[LLMEvent]: ...
    async def health(self) -> HealthStatus: ...

class TTSProvider(Protocol):
    name: str
    capabilities: TTSCapabilities          # 流式 / 采样率 / 音色列表

    async def synthesize_stream(self, req: TTSRequest, cancel: CancellationToken) -> AsyncIterator[AudioChunk]: ...
    async def health(self) -> HealthStatus: ...

class WakeWordProvider(Protocol):
    name: str
    capabilities: KwsCapabilities          # 多唤醒词 / 自定义词 / 采样率 / 帧长

    async def open_stream(self, cancel: CancellationToken) -> "KwsStream": ...
    def events(self) -> AsyncIterator[WakeEvent]: ...   # detected / error
    async def health(self) -> HealthStatus: ...
    async def warmup(self) -> None: ...     # 加载 ONNX 模型到内存，规避首次唤醒卡顿

class KwsStream(Protocol):
    async def feed(self, frame: AudioFrame) -> None: ...    # 逐帧喂入，帧长由 capabilities.frame_ms 决定
    async def close(self) -> None: ...

class IntentProvider(Protocol):
    name: str
    capabilities: IntentCapabilities       # 意图集合 / 是否支持槽位 / 是否可热切换

    async def classify(self, req: IntentRequest, cancel: CancellationToken) -> IntentResult: ...
    async def health(self) -> HealthStatus: ...
```

> **例外说明**：进程内 ONNX 推理仅限两类——唤醒词（`WakeWordProvider`，2~5 MB）与 VAD（音频算法，非 provider）。二者纯 CPU、待机常开不能容忍网络往返（FR-14.11、ADR-05、ADR-12）；ASR / LLM / TTS 仍严格走网络协议。

> 注意：除唤醒词外的模型 provider（ASR / LLM / TTS）均通过**网络协议**访问外部服务（本地或云端），不做进程内推理 —— 这是 CON-1 轻量化约束在接口层的体现；VAD 属音频算法，与唤醒词同为仅有的进程内 ONNX 例外。

> 意图识别约束（FR-17.10、FR-17.11）：`IntentProvider` 只接收 ASR 文本，不得为分类单独上传或缓存音频；实现可为规则、本地轻量分类器或 LLM 结构化输出。`stop` / `confirm` / `cancel` 的规则快速路径固定为进程内实现（不走网络），其余类别由可插拔驱动分类；驱动失败、超时或不可用时按 `chat` 降级。

### 7.2 驱动注册与解析

```python
@register_provider("asr", "funasr_local")
class FunASRLocalASR(ASRProvider): ...     # 内部通过 HTTP/WS 访问独立 FunASR 服务

@register_provider("asr", "dashscope_stream")
class DashScopeASR(ASRProvider): ...
```

新增厂商 = 新增驱动文件 + 注册装饰器 + 配置段，**编排层与状态机零改动**（FR-10.2）。

### 7.3 配置结构

```yaml
# config/app.yaml（音频与设备段，节选）
audio:
  transport: ros2_native           # ros2_native | ros2_bridge | file（ADR-02）
  device_profile: acr-robot-v2     # 机型 Profile 名 → config/devices/<name>.yaml（A1/A2、FR-16）
  auto_detect_profile: false       # 为空时是否按 robot_model 自动匹配（FR-16.3）
  fallback_profile: ""             # 自动探测失败时的兜底 Profile；为空则拒绝启动（FR-16.11）
  capture:
    target_sample_rate: 16000      # 适配层向上层输出的统一格式
    target_channels: 1
    target_bit_depth: 16
    jitter_buffer_ms: 60
  playback:
    queue_ms: 300                  # 播放队列上限（有界）
    flush_budget_ms: 100           # flush 调用到静音的预算（Barge-in 300ms 内）
  aec:
    mode: auto                     # auto（默认）| hardware | software | none（A4、CON-13）
    # auto：Profile.hardware_aec=true 且探测可用 → 用设备自带 AEC；否则自动切 software
    fallback_notice: true          # 模式与 Profile 声明不一致时，在日志 / WebUI 明确提示

# config/providers.yaml
providers:
  asr:
    active: funasr_local            # 热切换入口
    primary: funasr_local
    fallback: dashscope_stream
    drivers:
      funasr_local:
        endpoint: "http://127.0.0.1:10095"   # 独立 FunASR 服务
        sample_rate: 16000
        mode: "2pass"
        hotwords: ["充电桩", "A点", "会议室"]
      dashscope_stream:
        model: "paraformer-realtime-v2"
        api_key_env: "DASHSCOPE_API_KEY"
  llm:
    active: edge_llm                # 默认边端或云端；拓扑 2 可指向本机（A5、CON-12）
    primary: edge_llm
    fallback: openai_cloud
    drivers:
      edge_llm:
        base_url: "${LLM_BASE_URL}"       # 示例 http://10.0.0.12:8000/v1
        model: "Qwen2.5-7B-Instruct"
        stream: true
        supports_tools: true
      openai_cloud:
        base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
        model: "qwen-plus"
        api_key_env: "DASHSCOPE_API_KEY"
  knowledge:                        # 知识库检索（边端或云端，A5、FR-4.7）
    active: http_rag                # http_rag | none（关闭增强）
    drivers:
      http_rag:
        base_url: "${KB_BASE_URL}"   # 留空 = 关闭知识库增强（等价 active=none，启动 WARN 不阻断）
        top_k: 4
        max_chars: 1200
        timeout_ms: 200             # 计入首响（含在 LLM 首 token 800ms 预算内）；超时直接跳过，不阻塞对话
        api_key_env: "KB_API_KEY"
  tts:
    active: qwen3_tts_local
    drivers:
      qwen3_tts_local:
        base_url: "http://127.0.0.1:8091"
        voice: "default"
        sample_rate: 16000
      dashscope_tts:
        model: "cosyvoice-v1"
        api_key_env: "DASHSCOPE_API_KEY"
  wakeword:
    enabled: true                   # false → 启动即进入 ACTIVE_IDLE，退化为常开聆听（FR-14.8）
    active: openwakeword
    drivers:
      openwakeword:
        model_paths:                # 支持多唤醒词；至少含中文与英文各一（FR-14.14）
          - "assets/wakeword/wake_robot_v1.onnx"
          - "assets/wakeword/wake_robot_en_v1.onnx"   # 英文唤醒词（hello robot / hey robot）
        wake_phrases: ["你好机器人", "小智", "hello robot", "hey robot"]   # 仅用于提示与文档，实际以模型为准
        threshold: 0.6              # 置信度阈值，按现场标定
        cooldown_ms: 2000           # 命中冷却，防连续误触发
        sample_rate: 16000
        frame_ms: 80                # 帧长，与 KWS 模型输入对齐
    active_window_ms: 60000         # 会话活跃窗口（默认 60s），每轮有效交互后刷新；长任务规则见 FR-14.15
    preopen_asr: true               # 唤醒即预连接 / 预热 ASR，规避首句延迟
    confirm_twice: false            # 二次确认（播报「你是叫我吗？」）
    standby:
      announce: false               # 待机态任务结果是否直接播报；默认缓存 + 提示音（FR-14.16）
      announce_beep: true           # 待机态收到任务结果时的提示音
    open_ear:                       # 关闭唤醒词（常开聆听）时的约束（FR-14.8）
      cloud_asr_ack: false          # 默认本地 ASR；改用云端 ASR 须置 true 显式确认，否则配置校验 FAIL
    feedback:
      mode: "tts"                   # tts | beep | none
      text: "我在"
      beep_path: "assets/sfx/wake.wav"

language:
  mode: auto                        # fixed | auto（FR-15.3）
  fixed: zh                         # mode=fixed 时锁定语言
  supported: [zh, en, yue, dongbei, wuhan, shanghai, tianjin]
  fallback: zh                      # 驱动不支持目标语言时的降级语言
  switch_confirm_turns: 2           # 自动切语言所需连续一致轮数（防抖，FR-15.4）
  min_chars: 6                      # 短于该长度的句子不触发自动切换
  asr_routing:                      # 语言 → ASR 驱动；缺省使用 providers.asr.active
    yue: dashscope_stream
    dongbei: funasr_dialect
    wuhan: funasr_dialect
    shanghai: funasr_dialect
    tianjin: funasr_dialect
  tts_voices:                       # 语言 → 音色；缺失即降级并标记
    zh: { driver: qwen3_tts_local, voice: "default" }
    en: { driver: qwen3_tts_local, voice: "en_female" }
    yue: { driver: dashscope_tts, voice: "yue_female" }
    dongbei: { driver: qwen3_tts_local, voice: "default", fallback_marked: true }
    wuhan: { driver: qwen3_tts_local, voice: "default", fallback_marked: true }
    shanghai: { driver: qwen3_tts_local, voice: "default", fallback_marked: true }
    tianjin: { driver: qwen3_tts_local, voice: "default", fallback_marked: true }

# config/intent.yaml（独立配置文件，支持热更新；FR-17.1 ~ FR-17.12）
intent:
  enabled: true
  confidence_threshold: 0.6         # 低于阈值一律按 chat（FR-17.4）
  classify_timeout_ms: 200          # 语义分类超时按 chat，不阻塞首响（FR-17.3）
  gray_zone_top: 0.75               # P2 澄清区间上限（threshold ~ threshold + 0.15，FR-17.12）
  active: llm_intent                # 语义分类驱动；stop / confirm / cancel 规则快速路径为进程内固定实现
  intents:                          # 意图集合可增删、可热更新，禁止写死在编排代码（FR-17.2）
    chat:
      description: "闲聊与开放问答"
      examples: ["今天天气怎么样", "给我讲个笑话"]
    knowledge:
      description: "知识库 / 领域知识问询"
      examples: ["充电桩怎么用", "手册里怎么写"]
    command:
      description: "机器人控制指令"
      examples: ["去 A 点", "返回充电桩", "打开夹爪"]
    confirm:
      description: "确认（仅高风险工具确认窗口内生效）"
      examples: ["确认", "好的", "执行吧"]
    cancel:
      description: "取消（仅高风险工具确认窗口内生效）"
      examples: ["取消", "算了", "不用了"]
    stop:
      description: "停止播报 / 取消当前任务"
      examples: ["停一下", "别说了", "停止"]
  drivers:
    llm_intent:
      kind: llm                     # llm | local_classifier | rule；新增实现仅需注册驱动
      base_url: "${LLM_BASE_URL}"
      model: "${LLM_MODEL}"
      supports_slots: true

resilience:
  failure_threshold: 3               # 连续失败次数触发熔断
  cooldown_seconds: 30
  health_probe_interval: 30
  warmup_on_start: true
```

### 7.4 密钥注入

配置项只声明「密钥来源的环境变量名」，实际密钥由运行时环境注入：

```yaml
api_key_env: "DASHSCOPE_API_KEY"
```

密钥不得写入配置文件、不得进入镜像、不得在日志与 `/api/config` 响应中明文返回（FR-10.10、NFR-5）。

### 7.5 熔断降级状态

```
CLOSED ──连续失败达阈值──▶ OPEN ──冷却期满──▶ HALF_OPEN ──探测成功──▶ CLOSED
                                                    └──探测失败──▶ OPEN
```

- `OPEN` 期间请求直接路由到 `fallback` 驱动；若 fallback 亦不可用，使用固定兜底话术并通过 TTS 播报。
- `HALF_OPEN` 每 30s 探测一次主驱动，成功后回切并播报提示。
- 默认熔断阈值为「30 秒窗口内连续 5 次失败」、冷却 30 秒，均可配置（FR-10.7）。
- **拓扑 2（本体 LLM）故障**：本地 LLM 超时 / 崩溃 / OOM 同样走本熔断状态机；配置了远端备用 LLM 时自动切换，未配置则 `DEGRADED` + 兜底话术，30 秒内完成；本地 LLM 须设 cgroup 上限（FR-13.13）。

### 7.6 多语种与方言设计

> A8 已确认：中文 + 英文，方言为粤语 / 东北话 / 武汉话 / 上海话 / 天津话。
> 核心原则：**语言是数据，不是分支** —— 语言只在路由、提示词与音色层体现，状态机与全双工链路不变。

#### 7.6.1 语言标识

| `lang` | 名称 | 默认来源 |
| --- | --- | --- |
| `zh` | 普通话 | 本地 FunASR |
| `en` | 英语 | 本地 FunASR（或云端） |
| `yue` | 粤语 | 云端方言 ASR |
| `dongbei` | 东北话 | 外置方言模型 / 云端 |
| `wuhan` | 武汉话 | 外置方言模型 / 云端 |
| `shanghai` | 上海话 | 外置方言模型 / 云端 |
| `tianjin` | 天津话 | 外置方言模型 / 云端 |

#### 7.6.2 两种工作模式

| 模式 | 行为 | 适用 |
| --- | --- | --- |
| `fixed` | 配置锁定语言，ASR 按固定语言识别，不做自动切换 | 单一语言现场、追求最稳表现 |
| `auto`（默认） | 以 ASR 输出的 `lang` 为主，辅以文本脚本判定（汉字 / 拉丁字母占比），连续 `switch_confirm_turns` 轮一致才切换 | 多语言混合现场 |

防抖规则：
- 句子短于 `language.min_chars`（默认 6 字符）不触发自动切换；
- 连续 `switch_confirm_turns`（默认 2 句）一致才切换，单句噪声误识别不得切语言（FR-15.4）；
- 同一 ASR 驱动内切换只更新会话的 `lang` 上下文，**不重建 ASR 会话、不改状态机状态**；跨驱动切换（如本地 `zh` → 云端方言）允许一次受控重连，但须在防抖判定期间预连接目标驱动，保证切换后首响不劣于 NFR-1（FR-15.4）；
- 切换动作不视为一轮交互，不刷新唤醒活跃窗口。

混说与降级规则：
- 中英混说整句按字符占比判定 `lang`：任一语言占比 ≥70% 判为该语言，无任一语言达到 70%（含接近 50% 的均衡句）沿用会话当前 `lang`，不按语言切段（FR-15.13）；
- 方言音色缺失而降级普通话时，`lang` 保留识别语言用于统计，另记 `tts_lang=zh`、`tts_fallback=true`，并约束提示词输出普通话书面语，禁止输出方言文本（FR-15.14）；
- 方言 ASR 模型服务不可达 / 超时 → 本轮退回 `zh` 驱动、`lang=zh`、`language_fallback_count` +1 并 WebUI 告警，连续失败按 §7.5 熔断该方言驱动（FR-15.15）。

#### 7.6.3 能力协商与选路

| `lang` | 默认 ASR | 默认 TTS | 不可用时的降级 |
| --- | --- | --- | --- |
| `zh` | `funasr_local` | `qwen3_tts_local`（中文音色） | — |
| `en` | `funasr_local` | `qwen3_tts_local`（英文音色） | `zh` |
| `yue` | `dashscope_stream` | `dashscope_tts`（粤语音色） | `zh` + 标记 |
| 东北 / 武汉 / 上海 / 天津 | `funasr_dialect`（外置方言模型）或云端方言 ASR | 无专用音色 → 普通话音色 | `zh` + 标记 |

选路规则：
1. 若 `language.asr_routing[lang]` 有配置，直接使用该驱动；
2. 否则使用 `providers.asr.active`，并检查其 `capabilities.languages` 是否包含 `lang`；
3. 不包含 → 使用 `language.fallback`（默认 `zh`），记 `WARN` 日志与 `language_fallback_count` 指标，**不得静默返回错乱文本**（FR-15.5）。

#### 7.6.4 中英混说（Code-switch）

- **不按语言切段**：整句一次性送 ASR，避免在语言边界处截断导致语义丢失（FR-3.8）；
- 下游 TTS 仍按句切分合成，切分以标点与语义停顿为准，不以语言为准；
- 混说句的 `lang` 按主语言（字符占比）取，用于音色选择；
- 系统提示词要求模型保持用户主要语言回应，英文专有名词不翻译。

#### 7.6.5 提示词与记忆

- 系统提示词注入 `当前语言={{lang}}` 与「请使用该语言回答」；方言场景允许补充「可用普通话回答」。
- 每轮 `Turn` 记录 `lang`；跨语言追问不重写历史文本，由 LLM 自行完成跨语言理解（FR-15.10）。
- 工具调用参数不受语言影响：工具名与参数使用固定 schema，由 LLM 完成自然语言到参数的映射。

#### 7.6.6 唤醒词多语言

- `wakeword` 支持按语言挂载多套模型与短语（中文词、英文词各自独立）；
- `auto` 模式下全部加载并并行推理，命中任一即可唤醒；
- 方言发音变体通过额外模型或短语别名配置实现，不新增状态。

#### 7.6.7 语言相关指标

`language_switch_count`（语言切换次数）`language_fallback_count`（降级次数）`asr_success_rate_by_lang`（分语言识别成功率）`tts_fallback_count`（音色降级次数）

---

## 8. 多轮对话记忆

| 机制 | 设计 |
| --- | --- |
| 存储 | 默认 SQLite（零依赖），可选 Redis；接口 `MemoryStore` 可插拔 |
| 消息模型 | `system` / `user` / `assistant` / `tool` 四类，绑定 `session_id` 与 `turn_id` |
| 上下文组装 | 系统提示词 + 摘要记忆 + 滑窗内最近 N 轮消息 |
| 滑窗裁剪 | 按 token 预算从旧到新裁剪，保留系统提示词与最近轮次；预算按 LLM 上下文窗口的 70% 设定 |
| 摘要记忆（P1） | 被裁剪的历史按批次压缩为摘要，作为一条系统消息保留 |
| 打断处理 | 用户完整输入 + **仅已送入播放队列的助手文本片段** + `status=interrupted` + `spoken_ratio`；未播出的生成内容只进事件日志，不进对话记忆（FR-6.8） |
| 生命周期 | 支持过期回收（默认 7 天）、手动清除、导出 |
| 长期记忆（P2） | 从对话中抽取用户偏好与事实，存为键值并在后续会话召回 |

### 8.1 知识库检索（RAG，边端 / 云端）

知识库**不在本体**（A5、CON-12），应用只做检索调用与提示词注入。

触发条件：`intent=knowledge`（§9.6）时必触发；知识库启用（`knowledge.active != none`）时检索与意图分类并行发起，因此分类结果晚到也不会漏检索。其他意图不额外检索，避免无谓调用与预算占用。

```
ASR final ─▶ 知识检索 KnowledgeRetriever (timeout 200ms)
                │ 命中片段（含来源 / 得分）
                ▼
      提示词组装：system + 摘要 + 滑窗 + 知识片段（≤ max_chars）
                ▼
              LLM
```

| 项 | 设计 |
| --- | --- |
| 抽象 | `KnowledgeRetriever.retrieve(query, lang, budget) -> KnowledgeHit[]`，驱动可插拔（`http_rag` / `none`） |
| 时机 | 在 LLM 调用前并行执行（与记忆组装并行），不串行增加延迟 |
| 超时 / 失败 | 超时（默认 200ms）或 5xx → 跳过增强、正常对话，打 `rag_skipped` 标记并计数（FR-4.7） |
| 预算 | `max_chars` 限制注入体积；命中过多时按得分截断，避免挤压对话历史 |
| 可追溯 | 每轮记录命中段落的 `doc_id` / `score`，WebUI 可查看「本轮引用了什么」 |
| 多语言 | 检索 query 使用 ASR 原文；返回片段若与 `lang` 不符则丢弃并计数（避免跨语言噪声） |
| 安全 | 知识片段一律视为不可信数据，提示词中明确标注来源边界，防止片段内指令被当作系统指令执行 |

---

## 9. 语音任务指令与执行

### 9.1 执行流程

```
用户语音 ─▶ ASR final ─▶ 意图识别与路由（§9.6）
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
   chat（默认）         knowledge（RAG）       command（工具）
        │                     │                     │
        │             知识检索 / 注入               ▼
        │                     │          策略校验（白名单 / 参数校验 / 风险分级）
        │                     │                     │
        │                     │           高风险 → 二次确认播报
        │                     │                     │
        └────────────┬────────┘                     ▼
                     ▼                   执行适配器（ROS2；REST/MQTT 预留）
             LLM(带 tools) ──▶ tool_call            │
                     │                             ▼
                     └──────────────▶ 结果回灌 LLM ─▶ 自然语言播报
```

### 9.2 工具定义

```python
ToolDef(
    name="navigate_to",
    description="导航到指定地点",
    params_schema={...},       # JSON Schema，用于参数校验
    risk="high",               # low | medium | high
    timeout_s=120,
    adapter="ros2",
    target="/robot/navigate",  # 适配器目标（服务名 / URL / 主题）
)
```

### 9.3 异步与状态隔离

- 工具执行在 `EXECUTING` 状态下异步进行，由独立任务持有**独立 turn_id 上下文**。
- 执行期间对话管线仍可响应新的用户输入，不阻塞。
- 工具结果通过事件回灌到新一轮 LLM 调用，生成播报。
- 默认策略：打断播报**不取消**已下发的工具任务（物理动作不应被语音打断中止），但可配置为可取消。

### 9.4 安全策略

| 策略 | 说明 |
| --- | --- |
| 白名单 | 仅 `config/tools.yaml` 中启用的工具可被调用 |
| 参数校验 | 依据 `params_schema` 校验，越界直接拒绝并播报原因 |
| 风险分级 | `high` 风险工具须二次确认（播报确认问题，等待用户确认）。确认问题播报期间不启用 Barge-in，ASR 持续采集并将用户语音交给确认判定（规则或 LLM 判定确认 / 取消），不新建 turn、不取消待执行任务；`tools.confirm_timeout_ms`（默认 10s，可配）超时或无法判定时按取消处理并播报，不默认执行（FR-9.10） |
| 频率限制 | 高频重复调用被限流，防止误触发导致的动作风暴 |
| 审计 | 每次工具调用记录参数、结果、耗时、触发轮次 |

### 9.5 本期范围（A6）

- 本期只实现 **ROS2 执行适配器**（service / action）与基于 `config/tools.yaml` 的机器人工具集；REST / MQTT 适配器**不实现**，仅在框架层保留扩展点（`ToolAdapter` 契约 + 注册表 + 配置结构）。
- **IoT 设备控制暂不纳入本期范围**（开关、传感器读写等），待具体需求明确后再排期；届时只新增适配器实现与工具定义，不改编排层与状态机。
- 验收口径：C-87 只验证「新增适配器零改业务代码」的扩展性，不要求 IoT 真实执行。

### 9.6 意图识别与路由（R14、FR-17）

> 目标：ASR final 后先产出结构化意图，再决定 RAG / 工具 / 对话路径。意图识别**不新增状态、不阻塞首响、不成为单点故障**。

#### 9.6.1 意图结果模型与默认集合

| 字段 | 含义 | 约束 |
| --- | --- | --- |
| `intent` | 意图标识 | 必须来自 `config/intent.yaml` 的 `intents`，不得在编排代码写死业务枚举（FR-17.1、FR-17.2） |
| `confidence` | 置信度 0~1 | 低于 `intent.confidence_threshold` 一律按 `chat`（FR-17.4） |
| `slots` | 槽位 | `command` 至少含工具名与参数；`knowledge` 含检索 query |
| `source` | `rule` / `model` / `llm` | 规则快速路径记 `rule` |
| `latency_ms` | 判定耗时 | 快速路径与语义分类分别计量（FR-17.9） |

| 默认意图 | 含义 | 处理路径 | 说明 |
| --- | --- | --- | --- |
| `chat` | 闲聊与开放问答 | 常规 LLM 生成 | 低置信度、分类失败、窗口外 `confirm` / `cancel` 的兜底去向 |
| `knowledge` | 知识库 / 领域知识问询 | 知识检索注入 → LLM | 检索失败 / 超时按 FR-4.7 跳过 |
| `command` | 机器人控制指令 | 工具校验 → 二次确认 → 执行 | 产出工具名与参数，受 §9.4 白名单 / 参数 / 风险约束 |
| `confirm` | 确认 | 仅确认窗口内交给确认判定 | 窗口外按 `chat`，不得误触发待执行任务（FR-17.6） |
| `cancel` | 取消 | 仅确认窗口内交给确认判定 | 同上 |
| `stop` | 停止播报 / 取消当前任务 | 复用 Barge-in 取消路径 | 立即停播，按策略取消任务（FR-17.6） |

#### 9.6.2 双路径判定与首响预算

| 路径 | 覆盖意图 | 执行方式 | 时延预算 | 失败处理 |
| --- | --- | --- | --- | --- |
| 规则快速路径 | `stop` / `confirm` / `cancel` | 进程内关键词与上下文窗口判定 | **≤ 20ms，串行前置，计入首响**（NFR-1） | 未命中视为普通文本，继续语义分类 |
| 语义分类 | `chat` / `knowledge` / `command` | 可插拔 `IntentProvider`（LLM 结构化输出 / 轻量分类器） | **与 LLM / RAG 并行，不单独累加**；超时 200ms | 超时 / 失败 → `chat` + WARN + 计数（FR-17.3、FR-17.8） |

并行编排（不阻塞首响）：

1. ASR final 到达后先跑规则快速路径（≤ 20ms）。命中 `stop` 立即复用 §6.3 打断序列；`confirm` / `cancel` 仅在 `tools.confirm_window.open=true` 时交给确认判定，否则按 `chat`。
2. 其余文本同时启动三件事：语义分类任务、RAG 检索（`knowledge.active != none` 时，≤ 200ms）、默认 `chat` 的 LLM 请求准备。RAG 与分类并行；LLM 在检索完成（或跳过）后发出，对应 NFR-1「检索 + 首 token ≤ 800ms」；RAG 关闭时分类与 LLM 请求真正并行。
3. 分类结果在首块音频入队前返回且为 `command`：取消对话流，按工具路径执行（受 §9.4 校验与二次确认约束）。
4. 分类结果晚于首块音频：本轮按已在播的 `chat` 完成，记录 `intent_late_route_count`，不中途切换播报；`knowledge` 的检索不受分类时序影响（见第 2 条），晚到结果只影响展示与统计。
5. 分类结果缺失、超时或低置信度：一律按 `chat`。

#### 9.6.3 路由规则

| 意图 | 路由 | 约束 |
| --- | --- | --- |
| `knowledge` | 触发 `KnowledgeRetriever` 并按 FR-4.7 注入提示词 | 检索失败 / 超时跳过并计数 `rag_skipped`，不阻塞对话 |
| `command` | 工具名 / 参数经白名单、`params_schema`、风险等级校验；高风险先二次确认后执行 | 意图结果与工具校验冲突时**以校验结果为准**（FR-17.5）；未授权工具按 `chat` 并播报拒绝原因 |
| `chat` | 常规 LLM 生成 | 默认去向；不得把低置信度结果升级为 `command` |
| `confirm` / `cancel` | 仅在 FR-9.10 确认窗口内生效 | 窗口外按 `chat`；不新建轮次、不取消待执行任务 |
| `stop` | 立即停播并按策略取消当前任务 | 复用 Barge-in 取消路径（§6.3），不额外增加打断延迟 |

#### 9.6.4 语言与可靠性

- 意图识别使用 ASR 输出的目标语言文本（`zh` / `en` / 方言）；中英混说按主语言处理，不因语言切换丢弃或重复判定（FR-17.7、FR-15）。
- 分类驱动遵守 §7.5 熔断与能力声明；失败、不可用或超时降级 `chat` 并 WARN，不得中断对话、不得阻塞工具校验；降级次数计入 `intent_fallback_count`（FR-17.8、FR-17.11）。
- 只使用文本，不得为意图分类单独上传或缓存音频；规则与轻量实现优先，重型模型不得进入应用镜像（FR-17.10、CON-1）。

#### 9.6.5 可观测与 WebUI

- 每轮 `Turn` 记录 `intent` / `confidence` / `source` / `slots` / `latency_ms`（§11）。
- WebSocket 推送 `intent.detected`；会话面板展示本轮意图、置信度与来源，监控页展示意图分布、低置信度占比、降级次数与两段耗时（FR-17.9、§10）。
- 指标：`intent_distribution` `intent_low_confidence_ratio` `intent_fallback_count` `intent_latency_ms{path=rule|classify}` `intent_late_route_count`。

#### 9.6.6 可插拔与配置

- 意图集合、示例话术、阈值、驱动全部落在 `config/intent.yaml`，支持热更新；新增意图或驱动只改配置 + 注册实现，**不改状态机与编排**（FR-17.2、FR-17.11）。
- `stop` / `confirm` / `cancel` 的规则快速路径固定内置；语义分类驱动可在 `llm_intent` / `local_classifier` / `rule` 间切换，具备 capabilities、健康检查与熔断降级（§7.5）。
- P2：置信度落在灰区（`threshold ~ threshold + 0.15`）时，可先播一句澄清问句并重试，**最多一次**（FR-17.12）。

---

## 10. WebUI 与接口设计

### 10.1 页面设计

| 页面 | 核心内容 |
| --- | --- |
| 实时会话 | 状态机状态可视化、**唤醒词命中提示与活跃窗口倒计时**、**当前语种 / 方言标记**、**本轮意图与置信度（含来源与槽位）**、音频活动指示、ASR 流式转写、LLM 流式文本、播报状态、打断事件时间线 |
| 运行控制 | 开始 / 结束会话、手动打断、麦克风静音、文本输入兜底、**手动唤醒 / 立即休眠**、**语言切换（固定 / 自动）** |
| 模型配置 | 驱动查看与切换、参数编辑、连通性测试、应用与回滚、密钥来源说明（不显示明文）、**唤醒词模型与阈值 / 冷却期配置**、**语言路由与音色映射配置**、**意图集合 / 置信度阈值 / 分类超时配置** |
| 任务与工具 | 工具列表（含风险等级）、调用历史、参数与执行结果 |
| 记忆管理 | 会话列表、历史消息查看、清除单轮 / 整会话、记忆策略配置 |
| 监控 | 延迟分位数、打断成功率、错误率、状态流转日志、**意图分布与低置信度占比** |
| 运维诊断 | 一键自检、音频回环测试、provider 测试、强制复位、日志导出 |

### 10.2 WebSocket 事件协议

- 端点：`/ws/session/{session_id}`
- 消息格式：`{ "v": 1, "type": "<event>", "ts": <epoch_ms>, "turn_id": <int|null>, "payload": {...} }`

**服务端 → 客户端**

| type | payload 关键字段 |
| --- | --- |
| `state.changed` | `from` `to` `event` `reason` |
| `wake.detected` | `phrase` `score` `latency_ms` |
| `wake.active_window` | `remaining_ms` `refreshed_by` |
| `vad.level` | `rms_db` `noise_floor_db` `speaking` |
| `asr.partial` | `text` `lang` |
| `asr.final` | `text` `duration_ms` `lang` |
| `language.changed` | `from` `to` `reason`（`auto` / `manual`） |
| `intent.detected` | `intent` `confidence` `source`（`rule` / `model` / `llm`）`slots` `latency_ms` |
| `llm.delta` | `text` |
| `llm.tool_call` | `name` `args` |
| `tts.chunk` | `seq` `bytes` `latency_ms`（默认只发元数据，音频不外传） |
| `playback.state` | `playing` `queued_ms` |
| `barge_in` | `trigger` `stage_latency` `accepted` |
| `tool.result` | `name` `status` `result` |
| `metrics.snapshot` | 各阶段分位数 |
| `error` | `code` `message` `recoverable` |

**客户端 → 服务端**

| type | payload |
| --- | --- |
| `session.start` / `session.stop` | — |
| `wake.trigger` / `session.sleep` | — （手动唤醒 / 立即回到待机） |
| `language.set` | `lang` 或 `mode`（`fixed` / `auto`） |
| `interrupt` | `reason="manual"` |
| `text.input` | `text` |
| `mic.mute` / `mic.unmute` | — |
| `config.update` | `path` `value` |

### 10.3 REST 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 系统与各 provider 健康状态 |
| GET | `/api/ready` | 就绪探测（供编排系统） |
| POST | `/api/diagnostics/selfcheck` | 一键启动自检 |
| POST | `/api/diagnostics/audio-loopback` | 音频回环测试 |
| POST | `/api/session/{id}/reset` | 强制复位会话（运维用） |
| GET / POST | `/api/sessions` | 会话列表 / 创建会话 |
| GET / DELETE | `/api/sessions/{id}` | 会话详情 / 删除会话 |
| GET | `/api/sessions/{id}/turns` | 轮次历史（含打断标记与耗时） |
| GET | `/api/config` | 当前生效配置（密钥脱敏） |
| PUT | `/api/config` | 更新配置（校验 + 预检 + 热生效） |
| POST | `/api/config/rollback` | 回滚到上一版本配置 |
| GET | `/api/providers` | 驱动列表与 capabilities |
| GET | `/api/languages` | 支持的语言 / 方言列表与当前生效语言 |
| POST | `/api/languages/active` | 切换当前语言或语言模式（`fixed` / `auto`） |
| GET | `/api/intents` | 意图集合、置信度阈值、分类超时与当前生效驱动 |
| POST | `/api/intents/test` | 意图分类试运行（传入文本，返回 `intent` / `confidence` / `source` / `slots` / `latency_ms`；调试与现场标定用） |
| POST | `/api/providers/{kind}/switch` | 热切换驱动 |
| POST | `/api/providers/{kind}/{driver}/test` | 连通性测试 |
| GET | `/api/tools` | 工具列表（含风险等级） |
| POST | `/api/tools/{name}/invoke` | 手动触发工具（调试用） |
| GET | `/api/metrics` | 延迟分位数与打断统计 |
| GET | `/api/logs/events` | 会话事件流导出（用于回放） |
| GET | `/api/logs/download` | 日志打包下载（运维用） |

---

## 11. 数据模型

| 实体 | 关键字段 | 存储 |
| --- | --- | --- |
| `Session` | `id` `created_at` `ended_at` `state` `config_snapshot` `turn_count` | SQLite |
| `Turn` | `id` `session_id` `seq` `lang` `user_text` `asr_partials` `intent` `intent_confidence` `intent_source`(`rule`/`model`/`llm`) `intent_slots` `intent_latency_ms` `assistant_text` `rag_hits`(doc_id / score / skipped) `status`(`completed`/`interrupted`/`failed`) `timings`(partial / llm_first / tts_first / e2e) `barge_in`(bool) | SQLite |
| `Message` | `id` `session_id` `turn_id` `role` `content` `tokens` `created_at` | SQLite / Redis |
| `ToolCall` | `id` `turn_id` `name` `args` `result` `status` `duration_ms` | SQLite |
| `MemorySummary` | `session_id` `up_to_turn` `summary_text` | SQLite |
| `MetricSample` | `session_id` `stage` `latency_ms` `provider` `ts` | 内存环形缓冲 + 可选落盘 |
| `EventLog` | `session_id` `turn_id` `seq` `type` `payload` `ts` | JSONL 文件（按天轮转） |

---

## 12. 目录结构

```
RobotChat/
├── .trae/specs/full-duplex-voice-interaction/
│   ├── requirements.md          # 需求规格（做什么）
│   ├── design.md                # 技术设计（怎么做）— 本文档
│   ├── deployment.md            # 部署与运维方案
│   ├── checklist.md             # 验收清单
│   └── tasks.md                 # 实施任务拆分
├── config/                      # 配置模板（挂载进容器，不入镜像）
│   ├── app.yaml                 # 音频、状态机超时、barge-in、AEC 参数
│   ├── providers.yaml           # 模型厂商驱动配置（含 wakeword 段）
│   ├── intent.yaml              # 意图集合、置信度阈值、分类超时与意图驱动（§7.3、§9.6）
│   ├── tools.yaml               # 工具与设备接口映射
│   └── devices/                 # 机型 Profile（每机型一份，A1/A2、FR-16）
│       ├── generic_ros_audio.yaml
│       └── <robot-model>.yaml
├── assets/                      # 随镜像分发的轻量资产
│   ├── wakeword/                # 唤醒词模型（openWakeWord ONNX，~2~5 MB）
│   └── sfx/                     # 唤醒提示音等短音频
├── deploy/                      # 部署资产
│   ├── Dockerfile
│   ├── Dockerfile.bridge        # 备选方案 B 的桥接镜像
│   ├── docker-compose.yml
│   ├── docker-compose.models.yml# 本体语音模型服务编排（可选）
│   ├── systemd/robotchat.service
│   ├── cyclonedds.xml           # DDS 配置模板
│   ├── .env.example
│   └── scripts/
│       ├── install.sh           # 一键部署
│       ├── selfcheck.sh         # 启动自检
│       ├── upgrade.sh           # 升级
│       └── rollback.sh          # 回滚
├── src/robotchat/
│   ├── main.py                  # FastAPI 入口 + 生命周期管理
│   ├── cli.py                   # 运维 CLI（doctor / provider test / audio probe / wake test）
│   ├── core/
│   │   ├── events.py            # 事件模型与有界事件总线
│   │   ├── state_machine.py     # 声明式状态机 + 转移表 + 完整性校验
│   │   ├── orchestrator.py      # 全双工会话编排
│   │   ├── turn.py              # turn_id 与陈旧事件丢弃
│   │   ├── cancel.py            # 层级取消令牌
│   │   └── watchdog.py          # 状态超时与全局心跳
│   ├── audio/
│   │   ├── transport.py         # AudioTransport 抽象（容器如何触达 DDS）
│   │   ├── device.py            # AudioDeviceAdapter 抽象：采集 / 播放两契约（§3.6）
│   │   ├── adapters/            # 机型适配实现（每机型一份，可复用通用基类）
│   │   │   ├── base_ros_audio.py
│   │   │   └── <robot-model>.py
│   │   ├── ros2_native.py       # 方案 A：容器内 rclpy 直连 DDS
│   │   ├── ros2_bridge.py       # 方案 B：通过宿主机桥接节点
│   │   ├── file_io.py           # 本地文件回放 / 录制（自测）
│   │   ├── aec.py               # 回声消除（NLMS）
│   │   ├── vad.py               # VAD + 端点检测（ONNX）
│   │   ├── barge_in.py          # 打断检测器
│   │   ├── kws.py               # 待机态唤醒词检测调度（启停 / 阈值 / 冷却）
│   │   └── buffer.py            # 有界环形缓冲 / 抖动缓冲
│   ├── providers/
│   │   ├── base.py              # 抽象接口 + capabilities
│   │   ├── registry.py          # 驱动注册表 + 熔断降级 + 预热
│   │   ├── asr/                 # funasr_local.py / funasr_dialect.py / dashscope_stream.py
│   │   ├── llm/                 # openai_compat.py（覆盖 vLLM/Ollama/云端）
│   │   ├── tts/                 # qwen3_tts.py / dashscope_tts.py
│   │   ├── knowledge/           # http_rag.py（边端 / 云端知识检索，A5）
│   │   ├── intent/              # llm_intent.py（LLM 结构化输出）/ local_classifier.py，可插拔（§9.6）
│   │   └── kws/                 # openwakeword.py（进程内 ONNX，唯一例外）
│   ├── dialog/
│   │   ├── memory.py            # 多轮记忆（滑窗 + 摘要）
│   │   ├── rag.py               # 知识检索调度：预算、超时降级、命中追溯（§8.1）
│   │   ├── language.py          # 语言路由：lang 判定 / 防抖 / 能力协商 / 降级
│   │   ├── intent.py            # 意图规则快速路径（stop / confirm / cancel）+ 路由与低置信度兜底（§9.6）
│   │   ├── prompt.py            # 提示词组装（注入 lang 上下文）
│   │   └── session.py           # 会话生命周期
│   ├── tools/
│   │   ├── base.py              # 工具定义与风险等级
│   │   ├── registry.py          # 工具注册表
│   │   ├── policy.py            # 白名单 / 参数校验 / 二次确认 / 限流
│   │   ├── adapters/            # ros2_adapter.py / rest_adapter.py / mqtt_adapter.py
│   │   └── builtin/             # announce.py / robot_control.py / iot_control.py
│   ├── api/
│   │   ├── ws.py                # WebSocket 事件网关
│   │   ├── rest.py              # 配置 / 监控 / 工具 / 诊断 REST
│   │   ├── auth.py              # 口令鉴权
│   │   └── schemas.py           # Pydantic 模型 + 协议版本
│   ├── observability/
│   │   ├── logging.py           # 结构化日志
│   │   ├── metrics.py           # 延迟指标与分位数
│   │   └── diagnostics.py       # 自检与回环测试
│   └── config/
│       ├── loader.py            # 加载 + 环境变量覆盖 + 校验 + 回滚
│       └── schema.py            # 配置 schema
├── webui/                       # Vue3 控制台
│   ├── src/views/               # Session / Config / Tools / Memory / Metrics / Diagnostics
│   └── src/api/                 # REST + WS 客户端
├── tests/
│   ├── unit/
│   ├── contract/audio/          # 机型适配契约测试（采集/播放/flush/重连/重采样，§3.6.5）
│   ├── e2e/
│   └── benchmark/
└── pyproject.toml
```

---

## 13. 技术决策记录（ADR）

| ID | 决策 | 理由 | 备选方案 | 影响 |
| --- | --- | --- | --- | --- |
| ADR-01 | 应用以 Docker 容器交付，与宿主机 ROS2 在同一 DDS 域协同 | 满足 CON-2；环境一致、升级回滚简单；不污染机器人既有环境 | 裸机安装（污染环境、难回滚）、K8s（过重，单机不必要） | 引入容器编排复杂度；需处理 DDS 网络与 `/dev/shm` |
| ADR-02 | ROS2 接入默认采用「容器内 rclpy 直连 DDS」，备选「宿主机桥接节点」 | 直连延迟最低、组件最少；桥接方案用于解耦 Python 版本 | 仅桥接（延迟略高）、仅直连（版本被锁死） | 决定基础镜像与 Python 版本策略 |
| ADR-03 | Python 版本跟随 ROS2 发行版（Humble → 3.10，Jazzy → 3.12；矩阵见 ADR-15） | 避免 rclpy ABI 冲突，减少一个故障源 | 固定单一 Python 版本并用桥接方案 | 代码须兼容 3.10 ~ 3.12 语法与依赖下限（CON-10） |
| ADR-04 | 本地模型（ASR / TTS）作为**外部服务**，应用镜像不含推理框架；VAD / KWS 两类轻量 ONNX 为唯一例外 | 满足 CON-1 轻量化；模型可独立升级与重启；故障隔离（FR-13.10） | 模型内嵌容器（镜像臃肿、耦合、单点） | ASR / LLM / TTS 驱动均为网络客户端；唤醒词为唯一进程内模型 provider（ADR-12）；须做预热与连接池；按 A5 分层（ADR-17） |
| ADR-05 | VAD 采用 Silero VAD ONNX + onnxruntime（CPU） | 避免 PyTorch（数百 MB + 启动慢）；CPU 足够 | webrtcvad（准确率较低）、PyTorch 版 Silero（过重） | 需随镜像附带 ONNX 模型文件（~2 MB） |
| ADR-06 | 软件 AEC 用 NumPy 自实现 NLMS；`audio.aec.mode` 默认 `auto`（有硬件用硬件，无则软件） | 避免 scipy / speex 依赖，满足轻量化；A4 确认 AEC 能力因机型而异，必须可配置而非写死 | scipy.signal（重）、webrtc-audio-processing（编译复杂）、只支持单一模式（无法覆盖多机型） | 需自行保证数值稳定性与性能；`auto` 的选择结果须在健康接口与日志可见（CON-13） |
| ADR-07 | 状态机采用声明式转移表 | 可枚举、可审查、可做完整性校验；杜绝散落判断导致的乱响应 | 状态模式类（代码量大）、if/else（不可维护） | 需设计转移表 DSL 与校验器 |
| ADR-08 | 事件总线有界 + 单写者串行处理 | 消除状态转移竞态，防止队列无界增长 | 多协程并发处理（竞态风险） | 高吞吐事件（音频）不走此总线，直接走专用通道 |
| ADR-09 | 记忆默认 SQLite，可选 Redis | 单机零外部依赖，符合轻量化；Redis 用于多路并发场景 | 纯内存（重启丢失）、Postgres（过重） | SQLite 写入需异步串行化避免锁竞争 |
| ADR-10 | 通信协议为 WebSocket 事件流 + REST，且版本化 | 实时事件天然适配 WS；控制类操作 REST 更清晰 | 纯 WebSocket（控制语义混乱）、gRPC（浏览器不友好） | 需维护协议版本与兼容策略 |
| ADR-11 | 打断默认**不取消**已下发的工具任务 | 物理动作不应被语音打断中途中止，存在安全风险 | 打断即取消任务 | 需在 UI 明确展示任务仍在执行 |
| ADR-12 | 唤醒词默认采用 **openWakeWord（ONNX）** 进程内推理，可插拔替换 | 轻量（2~5 MB、纯 CPU）、Apache-2.0 无商业授权风险、支持自定义唤醒词训练；待机常开不能容忍网络往返（FR-14.11） | Porcupine（效果最好但需商业 License、自定义词收费）、自训 KWS 模型（成本高、需数据）、云端唤醒（隐私与延迟不可接受） | 镜像内仅容许 VAD 与唤醒词两类 ONNX 推理资产（ADR-05），须在依赖白名单中登记为 `local-kws`；自定义唤醒词需新训 ONNX 并替换 `model_paths`（R15） |
| ADR-13 | 多语言采用**能力声明 + 语言路由**；方言 ASR 走外置 / 云端模型，本地保 `zh` / `en` | 满足 A8，且不违反 CON-1 / CON-8：方言模型体积大、版本多，不进应用镜像；本地保留中英保证断网基础可用 | 单一超大模型覆盖全部方言（镜像与算力不可行）、全部走云端（断网不可用） | 驱动须在 `capabilities.languages` 声明能力；路由与降级须有单测；方言识别质量依赖外部服务（R16） |
| ADR-14 | 音频设备接入采用 **`AudioDeviceAdapter` 抽象 + 机型 Profile 配置化**，采集与播放拆为独立契约 | 满足 A1/A2 与 CON-11：各机型接口不同，硬编码必然导致「每接一机型改一次业务代码」；Profile 化让换机型只换配置 | 每机型分叉业务代码（维护灾难）、统一约定所有机型同一话题（现实中不存在） | 上层禁止出现具体话题 / 服务名；新增机型须通过契约测试；Profile 缺失时明确失败（R20） |
| ADR-15 | 维护 **Humble + Jazzy 双发行版镜像矩阵**，同一源码分别构建并打标（`-humble` / `-jazzy`） | 满足 A3 与 CON-10：两类机型都要交付，且 Python 版本被发行版锁定 | 单镜像兼容两发行版（Python 与 rclpy 无法同时满足）、仅支持单发行版（覆盖不足） | CI 双构建；代码兼容 3.10 ~ 3.12；启动校验 `ROS_DISTRO` 与镜像标签一致（R21） |
| ADR-16 | **算力分层**：语音模型（ASR / TTS / VAD / AEC / KWS）**默认**在本体，LLM 与知识库**默认**在边端或云端；本体算力不足或大算力机型可显式改配 | 满足 A5 与 CON-12：语音链路延迟与断网可用性要求最高，必须在本地闭环；LLM 对网络容忍度高且与语音模型抢资源会破坏打断与首响 | 全本地（本体资源不够、OOM 风险高）、全云端（断网不可用、打断与首响不达标） | provider 只改 `base_url` 即可迁移位置；本体须给出资源预算表（NFR-3、R22） |
| ADR-17 | LLM 与知识库统一采用 **OpenAI 兼容协议 + 可配置 `base_url`**；LLM 默认不在本体内运行 | 同一驱动即可在「边端 / 云端 / 可选本机小模型」之间切换，避免三套实现；边端迁移不改代码 | 为每类位置写一套驱动（维护成本高）、绑定单一厂商（无法替换） | 边端与云端须保持协议兼容；`timeout` 与降级策略按位置分档（NFR-1）；本机 LLM 须显式开启并通过资源校验 |
| ADR-18 | 意图判定采用**双路径**：`stop` / `confirm` / `cancel` 走进程内规则快速路径（串行前置，≤ 20ms），其余语义分类**与 LLM / RAG 并行**，200ms 超时或低于阈值一律兜底 `chat` | 意图是编排的前置决策，但绝不能成为首响阻塞项（NFR-1）；规则路径保证「说停就停」的确定性，语义分类并行保证准确率 | 全部用 LLM 判定（首响阻塞、`stop` 不可靠）、全部用规则（命令覆盖不足、维护成本高）、串行分类（首响劣化） | 意图集合与阈值外置配置；低置信度与确认窗口外的 `confirm` / `cancel` 一律按 `chat`，禁止误触发工具（INV-10、INV-11）；晚到的 `command` 不追溯执行（R26） |

---

## 14. 风险与缓解

| # | 风险 | 影响 | 缓解措施 |
| --- | --- | --- | --- |
| R1 | 无硬件 AEC 导致自打断 | 播报被反复打断，体验崩坏 | 软件 NLMS + 播报期门限提升 + 保护窗口 + 语义回声剔除；提供半双工兜底开关 |
| R2 | 跨容器 DDS 发现失败（本机最常见故障） | 采不到音频，系统像"哑巴" | 固定 `ROS_DOMAIN_ID`、`RMW_IMPLEMENTATION`、挂载 `/dev/shm`、`network_mode: host`；自检脚本专检此项（见 deployment.md §8.2） |
| R3 | 本地模型服务不可用（OOM / 崩溃） | 会话卡死 | 模型服务独立容器 + 熔断降级 + 兜底话术 + 自动回切 |
| R4 | 云端 API 限流或超时 | 首响延迟劣化 | 指数退避重试 + 超时熔断 + 降级到本地 |
| R5 | ASR 端点误判（用户停顿被当说完） | 对话被截断 | 尾点时长可调 + 最大时长保护 + 短文本语义补全 |
| R6 | 长任务执行期间用户继续说话导致状态混乱 | 乱响应 | `EXECUTING` 与对话管线解耦，turn_id 隔离，结果独立回灌 |
| R7 | 状态机死锁或事件丢失 | 卡死 | 全状态超时兜底 + 看门狗心跳 + 非法事件忽略告警 + 事件队列有界 |
| R8 | 打断过于灵敏导致频繁误打断 | 体验噪音 | 多帧确认 + 保护窗口 + 语义确认 + 阈值可配可调 |
| R9 | 机器人本体算力不足跑本地模型 | 延迟不达标 | 支持云端卸载；提供能力探测与自动选路策略 |
| R10 | 容器资源未限制导致影响机器人其他业务 | 机器人整体异常 | 设置 cgroup CPU / 内存上限；限制线程数；音频与 VAD 设为低优先级 |
| R11 | 镜像体积与依赖膨胀，违背轻量化约束 | 部署慢、维护难 | 依赖分层白名单 + CI 中校验镜像体积阈值 + 禁止推理框架进镜像 |
| R12 | 配置热切换引入不一致状态 | 运行异常 | 变更前置校验 + 预检连通性 + 原子生效 + 一键回滚 |
| R13 | 现场网络不可达公网 | 云端驱动不可用 | 启动自检探测外网可达性，不可达时自动锁定本地驱动并告警 |
| R14 | 唤醒词频繁误唤醒 | 无效唤醒、算力与云端费用浪费、用户困扰 | 置信度阈值 + 命中冷却 + 可选二次确认三者可配；按现场噪声标定阈值；待机态仅跑 KWS 不启 ASR，控制误唤醒代价；监控 `wake_false_positive` |
| R15 | 自定义唤醒词需重新训练模型 | 换词周期长、需录音数据 | 优先选用可自训的 openWakeWord；阈值与词长约束可先行缓解；配置校验对短词给出提示；换词仅替换模型文件与 `model_paths`，不改代码（FR-14.2） |
| R16 | 方言识别率不足（东北 / 武汉 / 上海 / 天津 资源稀缺） | 识别错乱、任务指令失效 | 方言默认走能力更强的云端或专用外置模型；明确标注支持等级；识别置信度低时播报澄清；提供 fixed 普通话兜底 |
| R17 | 方言 TTS 音色缺失 | 用户听到普通话，体验打折 | 音色映射缺失即降级普通话并标记（FR-5.7）；优先为高价值方言采购云端音色 |
| R18 | 中英混说被错误切分或翻译 | 语义丢失、指令解析失败 | 整句送 ASR 不按语言切段；提示词禁止翻译专有名词；混说句以字符占比定 `lang`（§7.6.4） |
| R19 | 语言自动判定抖动（短句 / 口音 / 噪声） | 音色与提示词频繁切换 | `min_chars` + `switch_confirm_turns` 双重防抖；抖动频繁时可切 `fixed` 模式 |
| R20 | 机型适配成本被低估（每新增一个机器人型号都要写适配 + 调参） | 交付周期拉长、适配质量参差 | 抽象层 + Profile 化收敛改动面（ADR-14）；提供通用 `base_ros_audio.py` 覆盖常见 topic/service 形态；契约测试作为准入闸门；把「适配新机型的工时」显式计入排期（Phase 1A） |
| R21 | Humble / Jazzy 的 API 与依赖差异（消息包名、rclpy 细节、Python 3.10 vs 3.12、第三方轮子可用性） | 一套代码在某一发行版上跑不通或行为不一致 | CI 双发行版构建 + 双跑单测；依赖只使用在两版本均可用且带 ARM64 轮子的库；启动校验 `ROS_DISTRO` 与镜像标签；必要时以桥接方案兜底（ADR-15） |
| R22 | 本体算力不足：ASR / TTS / VAD / AEC / KWS 同时驻留导致 CPU 打满或 OOM | 首响变慢、打断失效、甚至整机业务受影响 | 部署前实测预算（D13）；模型服务设 cgroup 上限；空闲卸载 ASR / TTS；优先降并发而非降质量；必要时云端 ASR 兜底（ADR-16） |
| R23 | 边端 / 云端 LLM 网络抖动导致首 token 超时 | 用户等待、轮次频繁降级 | 首 token 超时熔断 + 兜底话术；边端优先、云端兜底；连接复用与预热；按位置分档设置超时（NFR-1、ADR-17） |
| R24 | 知识库检索注入噪声或恶意片段 | 回答偏离、提示词被劫持 | `max_chars` + 得分截断；片段标注为不可信数据并限定来源边界；超时即跳过；命中可追溯（§8.1） |
| R25 | 意图误判（把闲聊识别为命令、把命令识别为闲聊） | 误触发机器人动作（安全风险）或该执行未执行（体验落差） | 置信度阈值默认 0.6 且低于阈值一律兜底 `chat`；`command` 仅在高置信度下生效并仍受工具白名单 / 二次确认约束；意图集合与示例话术现场标定（D15）；监控 `intent_distribution` 与 `intent_low_confidence_ratio`；P2 灰区最多一次澄清（§9.6.6） |
| R26 | 语义分类晚于首块音频入队，`command` 未能生效 | 用户指令被当作闲聊，需再说一遍 | `command` 须在首块音频入队前生效，晚到则按 `chat` 完成本轮并记录 `intent_late_route_count`；分类超时 200ms 与并行编排控制晚到概率；现场标定阈值与超时（§9.6.2、§9.6.5） |
