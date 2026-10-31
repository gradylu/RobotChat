# RobotChat 全双工语音交互系统 — 部署与运维方案

- 文档版本：v2.0
- 日期：2026-09-23
- 状态：待评审
- 定位：本文档回答「**怎么部署、怎么运维**」。需求见 [requirements.md](./requirements.md)，技术设计见 [design.md](./design.md)，验收见 [checklist.md](./checklist.md)

---

## 1. 部署总览

### 1.1 部署原则

| 原则 | 落地方式 |
| --- | --- |
| 容器化交付 | 应用以 Docker 镜像交付，镜像内含代码、依赖与 WebUI 静态产物 |
| 配置与镜像分离 | 配置通过只读卷挂载，同一镜像可部署到不同现场（FR-10.9）；机型差异由 `config/devices/*.yaml` 的 Profile 表达（FR-16.3） |
| 一机一发行版标签 | 镜像按 `ROS_DISTRO`（humble / jazzy）分别打标，部署脚本按宿主机发行版自动选择（CON-10） |
| 不污染机器人环境 | 不修改宿主机 Python 环境、不修改机器人 ROS2 工作空间 |
| 与 ROS2 共存 | 通过共享 DDS 域与宿主机 ROS2 节点通信，不替换、不重启机器人既有节点 |
| 算力分层 | 语音模型在本体、LLM 与知识库在边端或云端（A5、CON-12），本体不为 LLM 预留资源 |
| 轻量化 | 镜像不含推理框架；本地模型作为独立服务（CON-1、ADR-04） |
| 可回滚 | 镜像按版本打标签，配置带版本快照，一键回滚 |
| 可自检 | 启动即自检，输出 PASS / WARN / FAIL 结论 |

### 1.2 部署拓扑（三种可选形态）

**拓扑 1（推荐，默认）：语音模型在本体 + LLM / 知识库在边端或云端（A5）**

```
┌─────────────────────── 机器人本体（Linux ARM64） ───────────────────────┐
│                                                                        │
│  ┌────────────────────┐   DDS(host 网络)   ┌─────────────────────────┐  │
│  │ robotchat 容器      │◀──────────────────▶│ 宿主机 ROS2 节点         │  │
│  │ 编排/状态机/Provider│                    │  麦克风阵列 / 扬声器      │  │
│  │ WebUI 静态资源      │                    │  机器人控制服务           │  │
│  │ 端口 8080 (局域网)  │                    └─────────────────────────┘  │
│  └─────┬──────────────┘                                                 │
│        │ HTTP / WS (127.0.0.1)                                          │
│        ▼                                                                │
│  ┌──────────────────────────────────────────────┐                       │
│  │ 本体语音模型服务（独立容器 / 宿主机进程）        │                       │
│  │  FunASR(:10095) · Qwen3-TTS(:8091)            │                       │
│  │  VAD / AEC / KWS 在应用进程内（ONNX，轻量）     │                       │
│  └──────────────────────────────────────────────┘                       │
└────────────────────────────────────────────────────────────────────────┘
                    │ HTTP / WS（内网或公网）
                    ▼
┌─────────────── 边端服务器 或 云端 ───────────────┐
│  LLM（OpenAI 兼容，7B+） · 知识库检索 / RAG 服务   │
└──────────────────────────────────────────────────┘
```

**拓扑 2：全部模型在本体（仅大算力机型）**
适用于本体具备 GPU / NPU 且算力充裕的机型，把 LLM 也放本机（`LLM_BASE_URL=http://127.0.0.1:8000/v1`）。此时按 deployment §12 重新核算资源，语音模型与 LLM 必须各自设 cgroup 上限，避免争抢导致打断失效。

**拓扑 3：本体仅应用 + 全云端 / 边端模型**
本体算力极受限时使用：ASR / TTS 也走云端或边端（**须显式配置降级**，默认仍在本体，CON-12）。代价是断网即不可用，且首响受网络影响，须配合熔断与兜底话术。

### 1.3 拓扑选择建议

| 条件 | 推荐拓扑 |
| --- | --- |
| 本体可跑 ASR / TTS（A5 默认前提），要求离线语音可用、延迟敏感 | 拓扑 1（默认） |
| 本体具备 GPU / NPU，算力充裕且希望断网也能对话 | 拓扑 2 |
| 本体算力极受限、现场网络稳定 | 拓扑 3 |
| 边端与云端都可用，要求边端优先、云端兜底 | 拓扑 1 + 云端 LLM fallback |

---

## 2. 交付物清单

| 类别 | 交付物 | 说明 |
| --- | --- | --- |
| 镜像 | `robotchat:<version>-humble` / `robotchat:<version>-jazzy` | 应用主镜像，按 ROS2 发行版双构建（CON-10、ADR-15） |
| 镜像 | `robotchat-bridge:<version>-<distro>`（仅方案 B 需要） | 宿主机 ROS2 桥接节点镜像，按宿主机发行版构建 |
| 编排 | `deploy/docker-compose.yml` | 主服务编排 |
| 编排 | `deploy/docker-compose.models.yml` | 本体语音模型服务编排（可选；LLM / 知识库不在此文件） |
| 服务 | `deploy/systemd/robotchat.service` | 开机自启与故障重启 |
| 配置 | `config/app.yaml` `config/providers.yaml`（含 `wakeword` 段）`config/tools.yaml` | 配置模板 |
| 配置 | `config/devices/*.yaml` | **机型 Profile 模板**（采集 / 播放话题、消息类型、采样率、停播方式，FR-16） |
| 资产 | `assets/wakeword/*.onnx` `assets/sfx/*.wav` | 唤醒词模型与提示音，随镜像打包（更换唤醒词时替换） |
| 配置 | `deploy/.env.example` | 环境变量模板（含密钥占位） |
| 配置 | `deploy/cyclonedds.xml` | DDS 配置模板 |
| 脚本 | `deploy/scripts/install.sh` | 一键部署 |
| 脚本 | `deploy/scripts/selfcheck.sh` | 启动自检 |
| 脚本 | `deploy/scripts/upgrade.sh` / `rollback.sh` | 升级与回滚 |
| 文档 | `deployment.md`（本文档） | 部署与运维说明 |
| 工具 | `robotchat` CLI | 诊断与运维命令 |

---

## 3. 容器镜像设计

### 3.1 基础镜像选型

| 方案 | 基础镜像 | 体积（含依赖） | Python | 适用 |
| --- | --- | --- | --- | --- |
| **方案 A1（默认，现场为 Humble）** | `ros:humble-ros-base` | ~1.4 GB | 3.10 | 容器内直连 DDS，组件最少、延迟最低 |
| **方案 A2（默认，现场为 Jazzy）** | `ros:jazzy-ros-base` | ~1.4 GB | 3.12 | 同上，适配 Jazzy 机型（CON-10） |
| 方案 B | `python:3.11-slim` | ~330 MB | 3.11 | 需解耦 ROS2 或需要更高 Python 版本 |
| 方案 C | `debian:bookworm-slim` + `ros-<distro>-rclpy` apt | ~700 MB | 随发行版 | 需更小体积且仍要 rclpy |

> A1 / A2 是**同一份源码的两个构建目标**，不是两套实现：构建参数为 `ROS_DISTRO=humble|jazzy`，产出标签 `robotchat:<version>-<distro>`。镜像启动时校验 `ROS_DISTRO` 与标签一致，不一致直接 FAIL。
> 体积差异主要来自 ROS2 基础镜像本身。方案 A 用体积换取「零额外组件 + 最低延迟 + 最简部署」，是机器人场景的默认推荐。

### 3.2 镜像依赖白名单（强制）

| 允许 | 禁止（CI 中校验，出现即构建失败） |
| --- | --- |
| `fastapi` `uvicorn[standard]` `pydantic` `pyyaml` `numpy` `httpx` `websockets` `onnxruntime` `structlog` | `torch` `transformers` `funasr` `vllm` `tensorflow` `jax` |
| `rclpy`（由基础镜像 / apt 提供） | 任何模型权重文件（> 50 MB） |
| 可选 extras：`[cloud]` → `dashscope` / `openai` | 编译型音频库（除非无纯 Python 替代） |
| 轻量资产：Silero VAD ONNX（~2 MB）、唤醒词 ONNX（~2~5 MB）、提示音 WAV（< 1 MB） | 大语言模型 / 语音合成权重 |

> 镜像内**仅允许**两类模型推理资产：Silero VAD ONNX（~2 MB，ADR-05）与唤醒词 ONNX（2~5 MB，ADR-12），均因体积小且需进程内常驻推理；FunASR / Qwen3-TTS 作为本体的独立服务（A5），LLM 与知识库在边端 / 云端（不在本体）。

### 3.3 卷的结构（分阶段构建是镜像构建内部细节）

镜像采用多阶段构建：Node 阶段编译 WebUI → 运行阶段仅拷贝 dist、源码与 ONNX 模型。

### 3.4 镜像体积预算

| 组成 | 方案 A（Humble / Jazzy） | 方案 B |
| --- | --- | --- |
| 基础镜像 | ~1.20 GB | ~150 MB |
| Python 依赖（core） | ~120 MB | ~120 MB |
| onnxruntime | ~60 MB | ~60 MB |
| Silero VAD ONNX 模型 | ~2 MB | ~2 MB |
| 唤醒词 ONNX 模型 + 提示音 | ~5 MB | ~5 MB |
| WebUI 构建产物 | ~3 MB | ~3 MB |
| 应用源码 | < 1 MB | < 1 MB |
| **合计** | **~1.4 GB** | **~340 MB** |
| 验收上限 | ≤ 1.6 GB（NFR-4 方案 A 档） | ≤ 800 MB（NFR-4 方案 B 档） |

> 体积口径：以 `docker images` 显示的镜像（解压后）大小为准，不含构建缓存与宿主机侧模型服务（NFR-4）。

### 3.5 构建要点

- 先 COPY 依赖清单并安装依赖，再 COPY 源码，最大化利用层缓存。
- 安装依赖后立即清理 apt / pip 缓存（`--no-install-recommends`、`rm -rf /var/lib/apt/lists/*`、`PIP_NO_CACHE_DIR=1`）。
- 通过 `.dockerignore` 排除 `tests/`、`webui/node_modules`、`.git/`、`*.md`。
- ARM64 构建：优先在机器人本体原生构建（`docker build`），或使用 Buildx 交叉构建（`--platform linux/arm64`）。
- **双发行版构建矩阵**（CON-10）：CI 中并行构建 `ROS_DISTRO=humble` 与 `ROS_DISTRO=jazzy` 两个镜像；依赖须选用在两版本、ARM64 均有可用轮子的库；单测在两个镜像内各跑一遍（R21）。
- 镜像标签规范：`<version>-<distro>-<git-sha>`，同时保留 `<version>-<distro>`；`latest` 仅开发环境使用。
- 机型 Profile（`config/devices/*.yaml`）**不烤进镜像**，随配置卷挂载，便于现场替换与新增机型（FR-16.3）。

---

## 4. 模型服务部署（本体语音 + 边端 / 云端 LLM）

### 4.1 服务清单与端口约定

| 服务 | 用途 | 部署位置 | 默认端口 | 部署方式 |
| --- | --- | --- | --- | --- |
| FunASR Runtime | 流式 ASR（`zh` / `en`） | **机器人本体** | 10095 | 独立容器 / 宿主机进程 |
| FunASR 方言实例（可选） | 方言 ASR（东北 / 武汉 / 上海 / 天津 等） | 机器人本体 或 边端 | 10096 | 独立容器；一语言一实例或按模型支持合并 |
| 云厂商方言 ASR / TTS（可选） | 粤语及方言兜底、英文音色 | 云端 | — | 无需容器，通过公网接入（A9 已确认可访问公网） |
| Qwen3-TTS | 流式 TTS（`zh` / `en`） | **机器人本体** | 8091 | 独立容器 / 宿主机进程 |
| LLM（vLLM / Ollama / 云端 API） | 对话生成与工具调用 | **边端 或 云端** | 8000 / 11434 / — | 不在本体；通过 `LLM_BASE_URL` 接入（A5、CON-12） |
| 知识库检索 / RAG | 领域知识检索增强 | **边端 或 云端** | — | 通过 `KB_BASE_URL` 接入；可关闭（FR-4.7） |
| VAD / AEC / KWS | 语音活动检测、回声消除、唤醒 | **机器人本体** | — | 应用进程内（ONNX / NumPy，无独立容器） |

> 方言模型与多语言音色一律**不进应用镜像**（CON-8、ADR-13）。启动自检会探测每种已启用语言的 ASR / TTS 可用性（见 §7.3 第 15 项），以及边端 / 云端 LLM 与知识库连通性（第 17 项）。
> 本体只承担语音模型；若某机型算力充裕并选择拓扑 2，LLM 才可放本机，且须重新核算资源（§12.1）。

### 4.2 编排示例（本体语音模型，拓扑 1）

```yaml
# deploy/docker-compose.models.yml —— 仅本体语音模型；LLM / 知识库在边端或云端（A5）
services:
  funasr:
    image: funasr-runtime-sdk-cpu:latest
    restart: unless-stopped
    ports: ["127.0.0.1:10095:10095"]
    command: ["--model", "paraformer-zh-streaming", "--port", "10095"]
    deploy:
      resources:
        limits: { cpus: "2.0", memory: 2G }

  tts:
    image: qwen3-tts-server:latest
    restart: unless-stopped
    ports: ["127.0.0.1:8091:8091"]
    deploy:
      resources:
        limits: { cpus: "2.0", memory: 3G }
```

> 拓扑 2（LLM 也在本体）时，再追加 `llm` 服务并给足内存；LLM 不参与本体的常驻预算（§12.1）。
> 关键约束：本体服务端口一律绑定 `127.0.0.1`，不对局域网暴露；LLM / KB 使用外部地址与鉴权。

### 4.3 外置带来的运维要点

| 要点 | 说明 |
| --- | --- |
| 独立重启 | 模型服务可单独重启而不影响应用容器；应用通过熔断降级感知 |
| 预热 | 应用启动时对三类 provider 发送预热请求，避免首轮承担模型冷启动（ADR-04） |
| 健康探测 | 应用按 `health_probe_interval` 周期探测；连续失败触发熔断；远端 LLM / KB 另按网络探测（ADR-17） |
| 资源隔离 | 模型服务必须设置内存上限，避免拖垮机器人本体 |
| 版本独立 | 模型服务版本可独立升级，与应用版本解耦 |

---

## 5. 容器编排（Docker Compose）

### 5.1 主服务编排示例

```yaml
# deploy/docker-compose.yml
x-ros2-env: &ros2-env
  ROS_DOMAIN_ID: ${ROS_DOMAIN_ID:-42}
  RMW_IMPLEMENTATION: ${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}
  CYCLONEDDS_URI: file:///etc/cyclonedds/cyclonedds.xml

services:
  robotchat:
    # <distro> 由宿主机 ROS_DISTRO（humble / jazzy）决定，install.sh 自动写入 .env（CON-10）
    image: robotchat:${ROBOTCHAT_VERSION:-1.0.0}-${ROS_DISTRO:?set ROS_DISTRO to humble or jazzy}
    container_name: robotchat
    restart: unless-stopped
    network_mode: host          # 与宿主机 ROS2 共享 DDS 网络，必须
    ipc: host                   # 共享共享内存，DDS 大消息传输需要
    environment:
      <<: *ros2-env
      TZ: Asia/Shanghai
      DASHSCOPE_API_KEY: ${DASHSCOPE_API_KEY:-}
      ROBOTCHAT_CONFIG_DIR: /app/config
      ROBOTCHAT_DATA_DIR: /var/lib/robotchat
      ROBOTCHAT_LOG_DIR: /var/log/robotchat
      # 机型 Profile：每台机器人指向自己的型号文件（FR-16.3）
      ROBOTCHAT__AUDIO__DEVICE_PROFILE: ${ROBOTCHAT_DEVICE_PROFILE:-generic_ros_audio}
    volumes:
      - ../config:/app/config:ro                                    # 配置 + 机型 Profile 外置只读
      - ./cyclonedds.xml:/etc/cyclonedds/cyclonedds.xml:ro          # DDS 配置
      - robotchat-data:/var/lib/robotchat                           # SQLite / 记忆数据
      - robotchat-logs:/var/log/robotchat                           # 日志
      - /dev/shm:/dev/shm                                           # DDS 共享内存
    cap_drop: ["ALL"]
    security_opt: ["no-new-privileges:true"]
    deploy:
      resources:
        limits: { cpus: "2.0", memory: 800M }
        reservations: { memory: 300M }
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/ready').read()"]
      interval: 30s
      timeout: 5s
      start_period: 90s
      retries: 3
    logging:
      driver: json-file
      options: { max-size: "50m", max-file: "5" }

volumes:
  robotchat-data:
  robotchat-logs:
```

### 5.2 关键参数说明

| 参数 | 为什么必须 |
| --- | --- |
| 镜像标签含 `<distro>` | 同一份代码按 Humble / Jazzy 分别构建；用错标签会因 rclpy / Python 版本不匹配启动失败（CON-10） |
| `ROBOTCHAT_DEVICE_PROFILE` | 每台机器人的音频接口不同，必须指向对应机型 Profile；未配置且无法自动匹配时拒绝启动（FR-16.9）。**部署层**在 `.env` 使用简名，compose 映射为容器内 `ROBOTCHAT__AUDIO__DEVICE_PROFILE`，二者不要同时手写 |
| `network_mode: host` | DDS 依赖组播与动态端口，桥接网络下发现会失败，这是 ROS2 容器化最经典的坑 |
| `ipc: host` + `/dev/shm` 挂载 | CycloneDDS 默认使用共享内存传输；`/dev/shm` 默认仅 64 MB，音频流会因空间不足丢包 |
| `ROS_DOMAIN_ID` 一致 | 容器与宿主机必须同域，否则互相不可见 |
| `RMW_IMPLEMENTATION` 一致 | 容器与宿主机必须使用同一 RMW 实现（推荐 CycloneDDS），否则发现失败 |
| `CYCLONEDDS_URI` 指向统一配置 | 保证接口自动选择、水位线等参数一致 |
| `restart: unless-stopped` | 崩溃自动重启，满足 NFR-2 |
| `cap_drop: ALL` | 最小权限，应用无需任何特权能力 |

### 5.3 DDS 配置模板

```xml
<!-- deploy/cyclonedds.xml -->
<?xml version="1.0" encoding="UTF-8"?>
<CycloneDDS xmlns="https://cdds.io/config">
  <Domain id="any">
    <General>
      <!-- 关键：留空表示自动选择网络接口，禁止硬编码网卡名（否则换机/换网即失效） -->
      <Interfaces/>
      <AllowMulticast>true</AllowMulticast>
    </General>
    <Internal>
      <Watermarks>
        <WhcHigh>500kB</WhcHigh>
      </Watermarks>
    </Internal>
  </Domain>
</CycloneDDS>
```

> 经验教训：曾因硬编码网卡名（如 `eth1`）导致跨进程 DDS 发现失败，改为自动选择接口后解决。

### 5.4 宿主机 ROS2 侧需确认项

- 宿主机与容器使用相同的 `ROS_DOMAIN_ID`。
- 宿主机与容器安装相同或兼容的 RMW 实现（若宿主机使用 FastDDS，容器也须装 FastDDS；推荐统一为 CycloneDDS）。
- 宿主机 ROS2 用户与容器内用户对 `/dev/shm` 均有读写权限。
- 若启用防火墙，需允许 DDS 使用的组播与动态端口范围（本机回环为主，通常无需额外开端口）。

---

## 6. 配置与环境管理

### 6.1 配置分层

| 层 | 位置 | 是否入镜像 | 说明 |
| --- | --- | --- | --- |
| 默认配置 | `config/*.yaml` | 入镜像（作为模板与兜底） | 首次部署的默认值 |
| 现场配置 | 宿主机 `./config` 挂载到 `/app/config`（只读） | ❌ | 现场实际生效值 |
| 环境变量 | `deploy/.env` + 容器环境 | ❌ | 覆盖敏感项与环境差异项 |
| 运行时变更 | 通过 WebUI / API 写入 `/var/lib/robotchat/config_active.yaml` | ❌（数据卷） | 热更新的持久化结果 |

优先级：**环境变量 > 运行时变更 > 现场配置 > 默认配置**

### 6.2 环境变量清单

```bash
# deploy/.env.example
ROBOTCHAT_VERSION=1.0.0
ROS_DISTRO=humble          # humble | jazzy，决定拉取哪个镜像标签（CON-10）
ROBOTCHAT_DEVICE_PROFILE=generic_ros_audio   # 机型 Profile，见 config/devices/*.yaml（FR-16）
ROS_DOMAIN_ID=42
RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
TZ=Asia/Shanghai

# 密钥（禁止提交到版本库，禁止写入 config/*.yaml）
DASHSCOPE_API_KEY=
OPENAI_API_KEY=

# 覆盖配置项（示例）
ROBOTCHAT__PROVIDERS__LLM__ACTIVE=edge_llm
ROBOTCHAT__BARGE_IN__GUARD_MS=200

# 唤醒词（B-06 已确认需要唤醒词；置 false 可退化为常开聆听）
ROBOTCHAT__PROVIDERS__WAKEWORD__ENABLED=true
ROBOTCHAT__PROVIDERS__WAKEWORD__ACTIVE_WINDOW_MS=60000
ROBOTCHAT__PROVIDERS__WAKEWORD__DRIVERS__OPENWAKEWORD__THRESHOLD=0.6

# 语言与方言（A8 已确认：zh / en + 5 种方言）
ROBOTCHAT__LANGUAGE__MODE=auto
ROBOTCHAT__LANGUAGE__FALLBACK=zh

# 音频设备（A1/A2 已确认：各机型接口不同，靠 Profile 适配）
# Profile 部署层用简名 ROBOTCHAT_DEVICE_PROFILE（见 §5.2）；compose 映射为 ROBOTCHAT__AUDIO__DEVICE_PROFILE
ROBOTCHAT__AUDIO__TRANSPORT=ros2_native
ROBOTCHAT__AUDIO__AEC__MODE=auto          # auto | hardware | software | none（A4）

# 模型算力分层（A5 已确认：本体跑 ASR / TTS / VAD / AEC / KWS，LLM 与知识库在边端或云端）
LLM_BASE_URL=http://10.0.0.12:8000/v1     # 边端；云端填 https://...
LLM_MODEL=Qwen2.5-7B-Instruct
KB_BASE_URL=http://10.0.0.12:9000         # 留空 = 关闭知识库增强（等价 knowledge.active=none，启动 WARN 不阻断）
KB_API_KEY=
```

> 环境变量改名规则：`ROBOTCHAT__` 前缀 + 配置路径 `__` 分隔 + 大写。

### 6.3 密钥管理

| 方式 | 适用 | 说明 |
| --- | --- | --- |
| `deploy/.env`（权限 600） | 单机部署（默认） | 最简单，需保证文件权限与不提交版本库 |
| systemd `EnvironmentFile` + 受限权限 | 生产 | 与 systemd 集成，启动时注入 |
| Docker secrets | 多机 / 更强隔离 | Compose 支持，密钥以文件形式挂载 |

密钥在 `/api/config` 响应中一律脱敏为 `***`，日志中一律不输出。

### 6.4 多环境配置

| 环境 | 用途 | 特点 |
| --- | --- | --- |
| `dev` | 本地开发（Windows / 无硬件） | `audio.transport: file`，使用本地文件回放；provider 可用桩服务 |
| `sim` | 仿真 / 半实物 | 使用真实模型服务，音频走文件或虚拟设备 |
| `robot` | 机器人本体 | 真实 ROS2 音频链路 + 真实模型服务 |

通过 `ROBOTCHAT_ENV` 切换配置叠加（`config/app.yaml` + `config/app.robot.yaml` 深合并）。

---

## 7. 启动、就绪与自检

### 7.1 启动时序

```
T+0s    容器启动
T+0.5s  加载配置 + schema 校验 + 加载机型 Profile（含 ros_distro 一致性校验）
T+1s    注册驱动，建立 provider 连接池
T+1s    连接 ROS2（DDS 发现，最长等待 5s）
T+2s    加载 VAD ONNX 模型
T+2s    启动常开 VAD 采集任务
T+3s    发送 provider 预热请求（ASR / LLM / TTS 各一次）
T+5s    自检执行（环境/ROS2/音频/provider/资源/网络）
T+8s    /api/ready 返回 200，服务就绪
T+30s   WebUI 可访问，语音交互可用
```

- 就绪前 `/api/ready` 返回 503，`/api/health` 返回各子系统明细。
- `start_period: 90s` 给足模型预热与 DDS 发现时间。

### 7.2 健康检查分层

| 端点 | 语义 | 用途 |
| --- | --- | --- |
| `/api/health` | 各子系统明细（配置 / ROS2 / 音频 / 各 provider / 存储 / 磁盘） | 运维查看根因 |
| `/api/ready` | 是否可对外提供服务（就绪探测） | 编排系统、健康检查 |
| `/api/live` | 进程存活 | 容器存活探测 |

### 7.3 启动自检（`robotchat doctor`）

| # | 检查项 | 判定 | 失败影响 |
| --- | --- | --- | --- |
| 1 | 配置 schema 校验 | FAIL 阻塞启动 | 启动失败并打印具体字段错误 |
| 2 | 必填环境变量（密钥、ROS_DOMAIN_ID） | WARN / FAIL | 缺失密钥时云端驱动不可用（WARN）；ROS_DOMAIN_ID 缺失（FAIL） |
| 3 | ROS2 DDS 发现：能否发现音频 topic 与控制服务 | FAIL（方案 A）| 无法采集/播放，服务标记 DEGRADED |
| 4 | 音频采集活性：5 秒内是否收到音频帧 | FAIL | 提示检查麦克风与 topic 名称 |
| 5 | 播放回环测试（可选，会出声） | WARN | 提示检查扬声器与播放服务 |
| 6 | ASR provider 连通 + 首响探测 | FAIL / WARN | 下降级 |
| 7 | LLM provider 注册与配置校验（真实连通性由第 17 项探测） | WARN | 配置缺失时提示按第 17 项排查 |
| 8 | TTS provider 连通 + 首音频探测 | FAIL / WARN | 下降级 |
| 9 | 存储可写（数据卷 / 日志卷） | FAIL | 启动失败 |
| 10 | 磁盘水位（数据卷 / 日志卷） | WARN (≥80%) / FAIL (≥95%) | 告警 / 拒绝启动 |
| 11 | 内存与 CPU 余量 | WARN | 提示调整模型规模 |
| 12 | 公网可达性（云端驱动需要时） | WARN | 自动锁定本地驱动 |
| 13 | 唤醒词模型加载与推理（待机态 KWS 自检）；`enabled=false` 时常开聆听：跳过模型加载，但校验 ASR 本地性约束（云端 ASR 须已置 `wakeword.open_ear.cloud_asr_ack=true`，否则配置校验 FAIL；置位则 WARN 提示音频将持续上传至云端） | FAIL / WARN | 唤醒功能不可用；`enabled=true` 时 FAIL，`false` 时按上述规则校验（FR-14.8） |
| 14 | 唤醒词端到端（可选，需人工说唤醒词） | WARN | 提示检查阈值与麦克风增益 |
| 15 | 语言能力探测：逐语言检查 ASR / TTS 可用性 | WARN | 不支持的方言标记为不可用，运行时按 `language.fallback` 降级并告警 |
| 16 | 设备 Profile 与音频接口连通：Profile 是否加载、`ros_distro` 与镜像标签是否一致、声明的话题 / 服务是否真实存在、采集是否有数据、播放是否可 flush | FAIL | 机型不匹配或 Profile 缺失时拒绝就绪，提示运行 `cli audio devices` / `cli audio profile-test` |
| 17 | 边端 / 云端 LLM 与知识库连通：`LLM_BASE_URL` 可达、首 token 时延达标、`KB_BASE_URL` 可达（或已关闭） | WARN（默认）；`llm.required=true` 时 LLM 为 FAIL | LLM 不可达默认进入 DEGRADED 并播报兜底话术，不拒绝就绪；仅显式要求 LLM 时拒绝就绪；KB 不可达允许启动但跳过增强并告警（ADR-17） |
| 18 | AEC 模式确认：`audio.aec.mode=auto` 时的实际选择结果（hardware / software）、样本回声衰减是否达标 | WARN | 无硬件 AEC 且软件 AEC 未生效时给出明确提示与调参建议（CON-13） |

> 判定语义：`FAIL` 表示该子系统不可用——除标注「阻塞启动 / 拒绝就绪」外，服务进入 `DEGRADED` 继续运行；`WARN` 表示可降级运行。只有配置显式声明 `required` 时才以 `FAIL` 阻塞就绪（对应 FR-13.11 / NFR-2 的降级要求）。

输出示例：

```
[PASS] config schema              ok (3 files, 0 error)
[PASS] env vars                   ok (ROS_DOMAIN_ID=42)
[FAIL] ros2 dds discovery         0 topics found in 5s, expected >= 2
       hint: 检查 ROS_DOMAIN_ID / RMW_IMPLEMENTATION 是否与宿主机一致
       hint: 运行 `ros2 topic list` 于宿主机确认节点在线
[PASS] audio capture              ok (received 47 frames / 5s)
[PASS] device profile             ok (acr-robot-v2, ros_distro=humble, topics matched)
[WARN] audio loopback             skipped (--no-playback)
[PASS] provider asr               ok (funasr_local, first-partial 268ms)
[PASS] provider llm               ok (edge_llm @ 10.0.0.12, first-token 612ms)
[PASS] provider tts               ok (qwen3_tts_local, first-audio 331ms)
[PASS] wakeword                   ok (openwakeword, 2 phrases, score 0.87 on sample)
[PASS] languages                  zh/en ok; dialect drivers unavailable (offline) → fallback zh
[PASS] storage                    ok (writable, 12.4GB free)
[WARN] internet                   unreachable, cloud drivers locked to local
------------------------------------------------------------
RESULT: DEGRADED (1 FAIL, 2 WARN) — 服务已启动，语音功能不可用
```

自检结论语义：

| 结论 | 含义 | 行为 |
| --- | --- | --- |
| `PASS` | 全部通过 | 正常提供服务 |
| `WARN` | 存在非阻塞问题 | 启动并告警，功能可能降级 |
| `FAIL` | 存在阻塞问题 | 默认仍启动（便于运维排查）+ 进入 `DEGRADED`；可用 `--strict` 改为拒绝启动 |

---

## 8. 现场诊断与运维手册

### 8.1 常用命令

| 场景 | 命令 |
| --- | --- |
| 部署 | `cd /opt/robotchat/deploy && ./scripts/install.sh` |
| 启动 / 停止 | `docker compose up -d` / `docker compose down` |
| 重启应用 | `docker compose restart robotchat` |
| 查看日志 | `docker compose logs -f --tail=200 robotchat` |
| 一键自检 | `docker compose exec robotchat python -m robotchat.cli doctor` |
| provider 测试 | `... cli provider test asr funasr_local` |
| 音频采集探测 | `... cli audio probe --seconds 5` |
| 音频回环测试 | `... cli audio loopback --text "测试"` |
| 查看已注册机型 Profile 与当前生效项 | `... cli audio devices` |
| 机型 Profile 逐项验证（话题 / 服务 / 采集 / 播放 / flush） | `... cli audio profile-test --profile <name>` |
| AEC 模式与回声衰减验证 | `... cli audio aec-test --seconds 10` |
| 知识库检索连通与命中测试 | `... cli rag test --query "充电桩怎么用"` |
| 唤醒词测试（播放样本 / 现场喊话） | `... cli wake test --seconds 10` |
| 唤醒状态与活跃窗口 | `... cli wake status` |
| 语言能力列表 | `... cli language list` |
| 语言识别测试（指定语言采样） | `... cli language test --lang yue --seconds 10` |
| 会话强制复位 | `... cli session reset --session-id default` |
| 查看当前状态 | `... cli state` |
| 导出诊断包 | `... cli diagnose export --out /tmp/robotchat-diag.tar.gz` |
| 宿主机侧检查 topic | `ros2 topic list` / `ros2 topic hz /audio_raw` |
| 检查 DDS 域 | `echo $ROS_DOMAIN_ID`（宿主机）对比容器内 `env \| grep ROS` |

### 8.2 故障排查决策树

```
【现象】完全没反应 / 采不到音频
  ├─ 容器内自检 ROS2 发现为 FAIL？
  │    ├─ 是 → 检查 ROS_DOMAIN_ID 是否与宿主机一致
  │    │        └─ 一致 → 检查 RMW_IMPLEMENTATION 是否一致
  │    │              └─ 一致 → 检查 /dev/shm 是否挂载且可写
  │    │                    └─ 正常 → 宿主机执行 ros2 topic list 确认节点在线
  │    └─ 否 → 检查 config/app.yaml 中音频 topic / service 名称是否与设备一致
  └─ 采集活性 FAIL → 检查麦克风设备是否被机器人其他程序独占

【现象】方言识别不准 / 英文识别差
  ├─ 自检第 15 项（语言能力探测）是否显示该语言可用
  │    ├─ 不可用 → 检查是否已部署对应方言模型（FunASR 方言实例）或云端凭据是否有效
  │    └─ 可用 → 继续
  ├─ `cli language test --lang <code> --seconds 10` 检查识别结果
  ├─ `language.asr_routing` 是否把该语言指向了正确的驱动实例
  ├─ 是否被降级到 zh（看日志 language_fallback 与指标 language_fallback_count）
  └─ 方言资源稀缺时 → 改用云端方言 ASR，或固定普通话模式（`language.mode=fixed`）

【现象】语言自动切换过于频繁（音色忽中忽英）
  ├─ 增大 `language.min_chars`（默认 6）与 `switch_confirm_turns`（默认 2）
  ├─ 短句 / 口音场景 → 直接使用 `language.mode=fixed` 锁定当前语言
  └─ 检查 ASR 是否返回了错误的 lang（对比 `asr.final` 的 lang 与文本）

【现象】机器人用普通话回应方言用户
  ├─ 检查 `language.tts_voices[lang]` 是否有专用音色
  ├─ 无专用音色属预期降级（FR-5.7），需采购方言音色或接受普通话播报
  └─ 检查提示词是否注入了正确 lang（查看 llm 请求日志中的 system prompt）

【现象】喊唤醒词没反应
  ├─ 自检第 13 项（唤醒词模型加载）是否 PASS
  │    ├─ FAIL → 检查 assets/wakeword/*.onnx 是否随镜像打包；模型路径是否与配置一致
  │    └─ PASS → 继续
  ├─ `cli wake test --seconds 10` 是否能看到 score 输出
  │    ├─ 有 score 但偏低（< 阈值）→ 调低 wakeword.threshold（0.6 → 0.5）或提高麦克风增益
  │    └─ 无任何 score → 确认待机态 KWS 任务是否被启动（日志 kws start），
  │                          且 `wakeword.enabled` 是否为 true
  ├─ 是否处于活跃窗口内（ACTIVE_IDLE 下 KWS 关闭，属正常设计，直接说话即可）
  └─ 唤醒词发音与模型不匹配 → 更换模型或改用可自训唤醒词（D10）

【现象】频繁误唤醒（没人叫它却应答）
  ├─ 提高 wakeword.threshold（0.6 → 0.7，逐次微调）
  ├─ 增大 wakeword.cooldown_ms（2000 → 3000）
  ├─ 开启 wakeword.confirm_twice = true（命中后二次确认）
  ├─ 选择更长、更独特的唤醒词（≥ 3 音节），避免与日常对话撞词
  ├─ 检查环境噪声源（电视 / 广播 / 同名语音播报），必要时启用降噪
  └─ 若仍不可控 → 临时 wakeword.enabled = false 退化为常开聆听（ASR 全程常驻、不启用空闲卸载，CPU 与云端 ASR 费用上升；该模式默认本地 ASR，改用云端须置 `open_ear.cloud_asr_ack=true`，NFR-3 的 10% 空闲 CPU 指标不适用，见 FR-14.8）

【现象】唤醒后首句识别慢（约多出数百毫秒）
  ├─ 确认 wakeword.preopen_asr = true
  ├─ 确认 ASR provider 预热成功（自检第 6 项首响探测）
  └─ 检查模型服务是否因闲置被回收（必要时开启服务常驻）

【现象】无法打断 / 打断不灵敏
  ├─ 检查 barge_in.mode 是否为 disabled
  ├─ 检查 barge_in.guard_ms 是否过大（> 500ms 会显得迟钝）
  ├─ 检查 barge_in.threshold_boost_db 是否过大（> 12dB 会漏打断）
  ├─ 检查 audio.aec.mode；若为 software，确认 AEC 是否过度抑制了用户语音
  └─ 查看 WebUI 打断事件时间线，确认是否有 rejected 记录及拒绝原因

【现象】频繁自打断（机器人自己打断自己）
  ├─ 先看实际生效模式（自检第 18 项）
  ├─ 该机型有硬件 AEC → `audio.aec.mode = hardware`（或保持 auto，修正 Profile 的 `hardware_aec: true`）
  ├─ 无硬件 AEC → 保持 `auto` 走软件方案，并调大 threshold_boost_db
  ├─ 增大 barge_in.guard_ms（保护窗口）
  ├─ 开启语义确认 barge_in.semantic_check = true 并调低相似度阈值
  └─ 兜底：barge_in.mode = disabled（退化为半双工）

【现象】首次响应特别慢，之后正常
  └─ 模型冷启动 → 确认 resilience.warmup_on_start = true；
       容器 start_period 是否足够；模型服务是否在容器启动后才就绪

【现象】麦克风无数据（自检第 4 / 16 项 FAIL）
  ├─ 自检第 3 项 DDS 发现是否 PASS（不通先按上一节排查）
  ├─ `cli audio devices` 确认当前加载的 Profile 名是否是本机型
  │    ├─ 不是 → 修正 ROBOTCHAT_DEVICE_PROFILE（或 config/app.yaml 的 audio.device_profile）
  │    └─ 是 → 继续
  ├─ `cli audio profile-test --profile <name>` 查看哪一项 FAIL
  │    ├─ 话题不存在 → Profile 的 capture.topic 与现场不一致（`ros2 topic list` 对照）
  │    ├─ 消息类型不匹配 → Profile 的 msg_type / fields 与实际消息结构不符（`ros2 topic echo` 对照）
  │    └─ 有话题但无数据 → 麦克风被机器人其他程序独占，或采集节点未启动
  ├─ 采样率 / 声道不符 → 不必改代码，改 Profile 的 capture.sample_rate / channels（适配层负责转换）
  └─ 时间戳回退告警 → Profile 的 timestamp.source 改为 arrival

【现象】扬声器无声（自检第 5 / 16 项 FAIL）
  ├─ `cli audio loopback --text "测试"` 是否出声
  ├─ `cli audio profile-test` 的 playback 部分
  │    ├─ 话题 / 服务不存在 → 修正 Profile 的 playback.topic / service
  │    ├─ 服务拒绝（返回 false）→ 检查扬声器是否被静音、音量是否为 0（volume 段）
  │    └─ 收到数据但无声 → Profile 的采样率 / 声道 / 位深与实际设备不符
  └─ 声音断续 → 检查 jitter / 播放队列上限与设备缓冲（Profile 的 playback.latency_ms）

【现象】机型不匹配 / 换个机型后启动失败
  ├─ 日志提示 `device profile mismatch` 或 `ros_distro mismatch`
  │    ├─ ros_distro 不一致 → 拉取/使用正确的镜像标签（`robotchat:<version>-<distro>`）
  │    └─ Profile 未知 → 该机型尚未适配，需新增 config/devices/<model>.yaml 与适配器（FR-16.7）
  ├─ 未配置 Profile 且自动匹配失败 → 显式设置 ROBOTCHAT_DEVICE_PROFILE
  └─ 不要通过修改业务代码临时绕过：话题名 / 服务名只允许出现在 Profile 与适配器中（CON-11）

【现象】自打断严重（机器人被自己的播报打断）
  ├─ `cli doctor` 第 18 项看实际 AEC 模式
  │    ├─ mode=hardware 但设备实际未做 AEC → 该机型 Profile 应声明 `hardware_aec: false`，改用软件方案
  │    ├─ mode=software 但回声衰减 < 15dB → 检查 `echo_reference` 是否存在（FR-1.5）、检查播放到采集的延迟补偿
  │    └─ mode=none → 显式关闭了 AEC，改回 `auto`
  ├─ 仍误触发 → 抬高 `barge_in.threshold_boost_db` / 延长 `guard_ms` / 打开语义回声剔除
  └─ 极端噪声 → 临时 `barge_in.mode=disabled`（半双工）兜底

【现象】LLM / 知识库不可达（自检第 17 项 FAIL / WARN）
  ├─ `LLM_BASE_URL` 不可达 → 检查边端服务是否启动、网络与端口、鉴权头
  │    ├─ 边端故障 → 切云端 fallback（`providers.llm.fallback`），并确认熔断已生效
  │    └─ 云端也不可达 → 进入降级，播报兜底话术（不得卡死）
  ├─ 首 token 超时 → 按位置调 `timeout_ms.first_token`（边端 / 公网分档，NFR-1）
  ├─ `KB_BASE_URL` 不可达或超时 → 正常对话但跳过知识增强，检查 `rag_skipped` 计数（FR-4.7）
  └─ 本体 CPU 打满导致 ASR / TTS 变慢 → 见 §12.1 预算，降低并发或空闲卸载（R22）

【现象】回复内容与用户问题不匹配（乱响应）
  ├─ 检查日志中 turn_id 是否出现串轮（陈旧事件未被丢弃）
  ├─ 检查记忆是否正确裁剪（上下文是否溢出被静默截断）
  └─ 检查是否存在多路会话共用同一 session_id

【现象】系统卡死 / 无响应
  ├─ 查看 /api/health，确认当前状态机状态
  ├─ 查看状态停留时长，是否超过该状态超时上限
  ├─ 查看看门狗是否触发（日志中 watchdog 关键字）
  ├─ 执行 `cli session reset` 强制复位
  └─ 仍无响应 → `docker compose restart robotchat` 并从诊断包定位根因

【现象】容器反复重启
  ├─ `docker compose logs --tail=200` 查看退出原因
  ├─ 是否为 OOM（检查 memory limit 与模型服务内存占用）
  ├─ 是否为磁盘满（检查数据卷与日志卷水位）
  └─ 是否为配置校验失败（日志中 config schema 错误）
```

### 8.3 参数调优指引（现场）

| 参数 | 默认 | 调大影响 | 调小影响 |
| --- | --- | --- | --- |
| `vad.speech_confirm_frames` | 3 | 更抗噪，起音更迟钝 | 更灵敏，易被噪声触发 |
| `vad.silence_ms` | 700 | 更耐心，响应更慢 | 更急躁，易截断用户 |
| `barge_in.guard_ms` | 随 AEC 模式自适应（hardware 100 / software 200 / none 300） | 更抗自打断，打断更迟钝 | 打断更快，易自打断 |
| `barge_in.threshold_boost_db` | 6 | 更抗自打断，易漏打断 | 更易打断，易自打断 |
| `barge_in.confirm_frames` | 3 | 更抗误打断 | 打断更快，易误触发 |
| `barge_in.echo_similarity_threshold` | 0.8 | 更激进剔除回声，可能吃掉用户真实语音 | 更保守，可能残留自打断 |
| `audio.aec.mode` | auto | — | hardware 最优，none 最差 |
| `wakeword.threshold` | 0.6 | 更抗误唤醒，可能喊不动 | 更灵敏，误唤醒增多 |
| `wakeword.cooldown_ms` | 2000 | 更抗连续误触发，连续唤醒变慢 | 连续唤醒更顺，误触发风险高 |
| `wakeword.active_window_ms` | 60000 | 免唤醒对话更连贯，ASR 空转更久 | 更省资源，用户需频繁唤醒；`wakeword.enabled=false` 时不生效（常驻 `ACTIVE_IDLE`，FR-14.8） |
| `wakeword.standby.announce` | false | — | 置 true 可待机直接播报任务结果，但为半双工、不支持 Barge-in（FR-14.16） |
| `wakeword.open_ear.cloud_asr_ack` | false | — | 仅当关闭唤醒词且强需云端 ASR 时置 true；未置位时配置校验 FAIL（默认本地 ASR，FR-14.8） |
| `tools.confirm_timeout_ms` | 10000 | 给用户更多确认时间，高风险操作等待更久 | 确认更快结束；超时/无法判定按取消处理、不执行（FR-9.10） |

> 调参建议：每次只改一个参数，改后在真实场景下做 20 次打断测试并查看 WebUI 指标，确认效果后再调下一个。

---

## 9. 日志、监控与告警

### 9.1 日志

| 项 | 设计 |
| --- | --- |
| 格式 | 结构化 JSON（`structlog`），字段含 `ts` `level` `session_id` `turn_id` `state` `event` `latency_us` `provider` `msg` |
| 输出 | stdout（Docker json-file 驱动，50 MB × 5 轮转）+ JSONL 文件（`/var/log/robotchat`，按天轮转） |
| 分级 | `ERROR` 需人工关注；`WARN` 需观测；`INFO` 关键事件（状态转移、打断、驱动切换） |
| 敏感信息 | 密钥、用户原始音频一律不落日志 |
| 事件回放 | 会话全链路事件独立写 JSONL，可导出回放（FR-12.3） |

### 9.2 监控指标

| 分类 | 指标 |
| --- | --- |
| 延迟 | ASR partial / ASR final / LLM 首 token / TTS 首音频 / 端到端首响 / **Barge-in 响应**（各 P50 / P90 / P95） |
| 打断 | 打断次数、成功率（accepted / rejected）、各阶段耗时、回声抑制触发次数 |
| 状态机 | 各状态停留时长、转移次数、非法事件数、陈旧事件丢弃数、看门狗触发次数 |
| Provider | 调用次数、失败率、熔断次数、降级切换次数、平均响应时间 |
| 资源 | CPU、内存、容器内存上限使用率、队列深度、队列丢弃数 |
| 业务 | 会话数、轮次数、工具调用次数与成功率 |
| 唤醒 | 唤醒次数、误唤醒次数（活跃期内无指令超时折算）、唤醒延迟（命中→反馈开始）、活跃窗口到期次数 |
| 语言 | 各语言 ASR 成功率、语言切换次数（`language_switch_count`）、降级次数（`language_fallback_count`）、TTS 音色降级次数（`tts_fallback_count`） |

### 9.3 告警规则建议

| 级别 | 条件 | 处置 |
| --- | --- | --- |
| 严重 | `/api/ready` 持续 503 超过 2 分钟 | 立即介入 |
| 严重 | 容器 OOM 重启 | 检查内存上限与模型占用 |
| 严重 | 磁盘水位 ≥ 95% | 清理日志 / 扩容 |
| 警告 | Barge-in 响应 P95 > 300ms 持续 5 分钟 | 排查模型与音频链路 |
| 警告 | 某 provider 熔断 | 确认服务状态 |
| 警告 | 看门狗 1 小时内触发 ≥ 3 次 | 排查状态机异常 |
| 警告 | 磁盘水位 ≥ 80% | 关注日志增长 |
| 警告 | 队列丢弃计数增长 | 排查处理能力瓶颈 |
| 警告 | 唤醒误报率偏高（误唤醒 / 唤醒次数 > 20%，且样本 ≥ 20） | 提高阈值、加冷却、或更换唤醒词 |

### 9.4 指标暴露

- 默认通过 `/api/metrics` 提供 JSON（供 WebUI 使用）。
- 可选启用 Prometheus 文本格式 endpoint（`/metrics`），便于接入既有监控系统。

---

## 10. 数据持久化与备份

| 数据 | 位置 | 是否需备份 | 备份方式 |
| --- | --- | --- | --- |
| 配置（现场） | 宿主机 `./config` | ✅ 必须 | 版本库 / 定期打包 |
| 运行时变更配置 | `robotchat-data/config_active.yaml` | ✅ | 随数据卷备份 |
| 会话与记忆（SQLite） | `robotchat-data/*.db` | ✅ | 定期 `sqlite3 .backup` 或整卷快照 |
| 事件日志 | `robotchat-logs/*.jsonl` | ⬜ 按需 | 定期归档，保留 N 天 |
| 诊断包 | 按需导出 | ⬜ | `cli diagnose export` |

- 建议备份周期：配置变更即备份；数据卷每日一次；日志保留 14 天。
- 数据卷独立于容器生命周期，`docker compose down` 不会删除数据（除非显式 `-v`）。
- 提供 `deploy/scripts/backup.sh` 与 `restore.sh`（P1）。

---

## 11. 升级与回滚

### 11.1 升级流程

```
1. 备份配置与数据           ./scripts/backup.sh
2. 记录当前版本             docker compose images robotchat
3. 拉取 / 加载新镜像         docker compose pull  (或 docker load -i robotchat-x.y.z.tar)
4. 更新 .env 中的版本号      ROBOTCHAT_VERSION=x.y.z
5. 滚动重启                 docker compose up -d  (restart policy 生效)
6. 等待就绪                 curl -f http://127.0.0.1:8080/api/ready
7. 执行自检                 ./scripts/selfcheck.sh
8. 验证核心功能             打断 / 一轮对话 / 工具调用
9. 观察指标 10 分钟          确认无异常后完成
```

### 11.2 回滚流程

```
1. 修改 .env 回到上一版本号   ROBOTCHAT_VERSION=<previous>
2. docker compose up -d
3. 恢复配置快照              ./scripts/restore.sh --config <snapshot>
4. 校验就绪与自检
```

### 11.3 升级注意事项

| 事项 | 说明 |
| --- | --- |
| 配置兼容性 | 升级前比对新增 / 变更的配置项；缺失项由默认配置兜底，多余项告警提示 |
| 数据迁移 | SQLite schema 变更须提供迁移脚本，升级前自动备份数据库 |
| 模型服务 | 模型服务版本可独立升级，但须在升级后执行一次连通性与延迟自检 |
| 停机窗口 | 单实例部署存在秒级停机；如不可接受，需采用双实例 + 端口切换（P2） |
| 版本固化 | 生产环境禁止使用 `latest` 标签，必须使用明确版本号 |

---

## 12. 资源限制与性能保障

### 12.1 资源预算

| 组件 | CPU 上限 | 内存上限 | 说明 |
| --- | --- | --- | --- |
| robotchat 应用容器 | 2.0 核 | 800 MB | 编排 + VAD + AEC + 协议适配 + 唤醒词推理（待机态 KWS 约占 0.1~0.2 核） |
| FunASR 服务（本体） | 2.0 核 | 2 GB | 按模型规模调整 |
| Qwen3-TTS 服务（本体） | 2.0 核 | 3 GB | 按模型规模调整 |
| **本体内合计** | **≤ 6 核** | **≤ 6 GB** | 语音链路全部就位；须在 D13 实测余量内（A5、NFR-3） |
| LLM / 知识库（边端或云端） | 不占本体 | 不占本体 | 远端资源，仅计入网络时延预算（ADR-16） |
| 实测校验（D13） | — | — | 部署后 `docker stats --no-stream` 对照本表上限；偏差 > 20% 需复测并更新预算 |

> 拓扑 2（LLM 也在本体）时按机型实测追加预算（7B 量化通常需 4 核 + 8 GB），且必须与语音模型分容器设 cgroup，禁止共用同一内存池。
> 拓扑 2 故障处理：本机 LLM 因 cgroup 上限被 OOMKill 或推理持续超时时，按 FR-10.7 熔断；已配置远端备用 LLM 则 30s 内切过去，未配置则 `DEGRADED` + 兜底话术；语音链路（ASR / TTS / VAD / KWS）不受影响（FR-13.13）。

### 12.2 保障措施

| 措施 | 说明 |
| --- | --- |
| 容器限额 | 应用容器设置 CPU / 内存上限，防止影响机器人其他业务（R10） |
| 线程限制 | 限制 onnxruntime 与 NumPy 线程数（`OMP_NUM_THREADS`），避免线程风暴抢占 CPU |
| 队列有界 | 所有内部队列设上限，超限丢弃并告警（INV-7、FR-12.5） |
| 音频优先级 | 采集与 VAD 任务设为高优先级，播放队列设上限避免积压 |
| 空闲降耗 | 空闲时 VAD 以最小算力运行，CPU 占用目标 ≤ 10% |
| 定期压测 | Phase 8 执行延迟基准测试，确认达标后可发布 |

---

## 13. 安全加固

| 项 | 措施 |
| --- | --- |
| 运行用户 | 容器内以非 root 用户（uid 1000）运行 |
| 权限最小化 | `cap_drop: ALL` + `no-new-privileges` |
| 文件系统 | 配置以只读方式挂载；应用仅对数据卷与日志卷有写权限 |
| 网络暴露 | 仅 WebUI 端口对局域网暴露；模型服务端口仅绑定 `127.0.0.1` |
| 鉴权 | WebUI 口令认证；写操作鉴权（配置变更、任务执行、强制复位） |
| 密钥 | 环境变量 / secret 注入；配置文件与镜像不含密钥；日志与 API 响应脱敏 |
| 指令安全 | 工具白名单 + 参数校验 + 高风险二次确认 + 调用限流 |
| 更新 | 基础镜像定期更新安全补丁并重新构建 |
| 审计 | 配置变更、工具调用、强制复位均记录审计日志 |

---

## 14. 部署验收

部署相关验收项见 [checklist.md](./checklist.md) §9（C-110 ~ C-134）、§10（C-140 ~ C-148）、§12（C-160 ~ C-168，机型适配）与 §13（C-169 ~ C-176，算力分层 / AEC / 知识库）。交付前须完成：

- 一键部署脚本在干净环境（仅含 Docker 的机器人本体）验证通过；
- 启动自检全部 PASS（或 WARN 项有明确说明），含唤醒词模型加载项；
- 容器重启后配置与历史数据不丢失；
- 升级与回滚流程演练通过；
- 资源限制生效，机器人其他业务不受影响；
- ROS2 既有节点功能未被破坏；
- 待机态唤醒可用且误唤醒率在验收阈值内（见 checklist §10）；
- 中英双语与 5 种方言按 checklist §11 验收通过，未支持的方言降级路径有明确提示。

---

## 15. 待确认事项

| # | 事项 | 影响 | 阻塞 |
| --- | --- | --- | --- |
| D1 | ~~机器人本体是否已安装 Docker / Docker Compose，版本号~~ **已确认（A10）：已安装 Docker**；仍需确认 Compose 版本 | Compose 语法版本 | T-7.1 |
| D2 | ~~机器人 ROS2 发行版本（决定基础镜像与 Python 版本）~~ **已确认（A3）：主要为 Humble 与 Jazzy，均须支持** | 双发行版镜像矩阵（CON-10、ADR-15），无残余风险 | T-0.7、T-7.1 |
| D3 | 宿主机使用的 RMW 实现（CycloneDDS / FastDDS） | DDS 兼容配置 | T-7.2 |
| D4 | 机器人本体 CPU 架构（ARM64 / x86_64）与算力 | 镜像构建方式与模型规模 | T-7.1 |
| D5 | 机器人本体内存与磁盘余量 | 模型服务能否本地部署 | T-7.3 |
| D6 | ~~现场网络：是否可达公网、是否需代理~~ **已确认（A9）：内网环境，可访问公网**；是否需代理仍待确认 | 云端驱动可用性 | T-7.3 |
| D7 | 机器人既有 ROS2 服务的启动方式（是否 systemd / launch） | 启动顺序与依赖协调 | T-7.4 |
| D8 | WebUI 是否需要固定分辨率全屏展示 | 前端适配 | T-6.11 |
| D9 | 是否需要多实例 / 高可用部署 | 是否需要更复杂编排方案 | T-7.4 |
| D10 | 唤醒词内容（默认示例词还是定制词）、**中英双语唤醒词资产**（FR-14.14）、是否需自训模型、是否采用商用引擎 | 决定 `assets/wakeword/` 资产来源与是否需训练流程 | T-3A.2 |
| D11 | 5 种方言各自的质量要求与可用模型来源（本地方言模型 / 云端方言 ASR），以及是否需方言音色 | 决定是否需额外部署方言模型容器、是否依赖公网、验收阈值 | T-2A.2、T-2A.3 |
| D12 | 各机型音频接口契约清单：麦克风话题名 / 消息类型 / 字段、扬声器播放方式（topic / service / action）、停播方式、采样率 / 声道 / 位深、时间戳来源；首批需适配的机型清单 | 决定 `config/devices/*.yaml` 内容与适配器实现范围（FR-16.2）；缺失时无法交付对应机型 | T-1A.1 ~ T-1A.4 |
| D13 | 首批机型本体算力实测：可用 CPU 核数、内存余量、是否具备 GPU / NPU；ASR / TTS / VAD / AEC / KWS 同时驻留的实测占用 | 决定本体资源预算与是否需云端兜底、是否需要空闲卸载（A5、NFR-3、R22） | T-2.11、T-7.3 |
| D14 | 边端 / 云端 LLM 与知识库服务契约：地址、模型名、鉴权方式、上下文窗口、是否 OpenAI 兼容、知识库检索接口与返回格式 | 决定 provider 配置与 RAG 驱动实现（ADR-17、FR-4.7） | T-2.11、T-5.12 |
