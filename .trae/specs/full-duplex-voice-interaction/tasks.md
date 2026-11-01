# RobotChat 全双工语音交互系统 — 实施任务清单

- 关联文档：[requirements.md](./requirements.md) · [design.md](./design.md) · [deployment.md](./deployment.md) · [checklist.md](./checklist.md)
- 说明：任务按依赖顺序排列。`P0` 为交付阻塞项。每个任务给出产出物、验证方式与对应的验收项。

---

## Phase 0 — 项目骨架、配置体系与容器化地基

> 容器化骨架提前到 Phase 0，避免后期为适配容器边界而返工（尤其是 Python 版本与音频链路形态）。

| ID | 优先级 | 任务 | 产出物 | 验证 | 对应验收 | 依赖 |
| --- | --- | --- | --- | --- | --- | --- |
| T-0.1 | P0 | 初始化 Python 项目：目录结构、`pyproject.toml`、核心依赖（fastapi / uvicorn / pydantic / pyyaml / numpy / httpx / websockets / onnxruntime / structlog）、ruff + mypy 配置 | `pyproject.toml`、`src/robotchat/` 骨架 | `pip install -e .` 成功；`ruff` / `mypy` 可运行 | C-105 | — |
| T-0.2 | P0 | **确认双发行版策略（A3 已确认）**：Humble + Jazzy 均须支持 → 定稿双镜像矩阵与 Python 3.10~3.12 兼容基线（更新 design.md ADR-03 / ADR-15） | 决策记录 | 两个发行版的基础镜像、标签、依赖来源明确 | B-08 | — |
| T-0.3 | P0 | 实现配置加载器：YAML 加载 + 多环境叠加 + 环境变量覆盖 + schema 校验 + 密钥脱敏 | `config/loader.py`、`config/schema.py`、`config/*.yaml` | 单测：非法配置被拒；环境变量可覆盖；`/api/config` 无明文密钥 | C-54、C-57 | T-0.1 |
| T-0.4 | P0 | 定义抽象接口与能力协商：`ASRProvider` / `LLMProvider` / `TTSProvider` / `VadEngine` / `WakeWordProvider` / `KnowledgeRetriever` / `AudioTransport` / `MemoryStore` / `ToolAdapter` + capabilities（设备适配契约见 T-1A.1） | `providers/base.py`、`audio/transport.py`、`tools/base.py` | 类型检查通过；接口语义清晰 | C-50 | T-0.1 |
| T-0.5 | P0 | 实现驱动注册表：`@register_provider(kind, name)` + 按配置实例化 + 未知驱动报错 | `providers/registry.py` | 单测：注册与实例化；未知驱动名报明确错误 | C-51、C-52 | T-0.4 |
| T-0.6 | P0 | 可观测性基础设施：结构化 JSON 日志（含 session_id / turn_id / state）、延迟指标采集与分位数计算（首响以尾点判定完成为起点，同时单独记录尾点判定时长） | `observability/logging.py`、`metrics.py` | 日志为 JSON；指标可查 P50 / P90 / P95；尾点判定时长与首响两项独立可见 | C-43、C-103 | T-0.1 |
| T-0.7 | P0 | **容器化地基**：`deploy/Dockerfile`（含 WebUI 多阶段构建，参数化 `ROS_DISTRO`）、`docker-compose.yml`、`cyclonedds.xml`、`.env.example`、`.dockerignore` | `deploy/` 全套骨架 | Humble 与 Jazzy 各能构建并启动；空服务可响应 `/api/health` | C-110、C-111、C-166 | T-0.2、T-0.1 |
| T-0.8 | P0 | **ROS2 共存打通**：`network_mode: host` + `ipc: host` + `/dev/shm` + `ROS_DOMAIN_ID` / `RMW_IMPLEMENTATION` / `CYCLONEDDS_URI` 同域验证 | 容器内可发现宿主机 ROS2 topic | 容器内 `ros2 topic list` 能列出宿主机节点；`cyclonedds.xml` 使用 `<Interfaces/>` | C-117、C-121 | T-0.7、B-09 |
| T-0.9 | P0 | 依赖白名单与镜像体积守门：CI 校验禁用依赖 + 体积阈值 | CI 校验脚本 | 出现 `torch` 等依赖时构建失败；体积超限告警 | C-112、C-113 | T-0.7 |
| T-0.10 | P0 | FastAPI 应用入口与生命周期管理（启动加载配置与驱动、优雅关停） | `main.py` | `/api/health` 返回系统与 provider 状态 | C-124 | T-0.3、T-0.5 |

---

## Phase 1 — 音频链路（全双工的物理基础）

> 本阶段完成「内部管线 + 通用 ROS2 适配」，机型差异的处理在 Phase 1A。

| ID | 优先级 | 任务 | 产出物 | 验证 | 对应验收 | 依赖 |
| --- | --- | --- | --- | --- | --- | --- |
| T-1.1 | P0 | 定义 `AudioFrame` 模型与重采样 / 格式转换工具；实现有界环形缓冲与抖动缓冲 | `audio/buffer.py` | 单测：重采样正确；超限按策略丢弃并告警 | C-28 | T-0.4 |
| T-1.2 | P0 | 实现 ROS2 音频采集适配（方案 A：容器内 rclpy）：订阅麦克风 topic → `AudioFrame` 流；断线自动重连（**话题 / 消息由机型 Profile 提供，见 Phase 1A**） | `audio/ros2_native.py`（采集） | 连续录制 30s 无丢帧；断连后自动重连 | C-118 | T-0.8、B-01 |
| T-1.3 | P0 | 实现 ROS2 播放适配：音频块 → 播放 topic / service；支持 `flush()` 立即停播（**播报方式由机型 Profile 提供，见 Phase 1A**） | `audio/ros2_native.py`（播放） | 播放可听；`flush()` 后 ≤ 100ms 静音 | C-119 | T-1.1、B-02 |
| T-1.4 | P0 | 实现本地文件回放 / 录制适配器（开发与现场诊断的自测通道） | `audio/file_io.py` | 不接 ROS2 也能跑通完整链路 | C-92、C-123 | T-1.1 |
| T-1.5 | P0 | 实现 VAD：Silero VAD ONNX + 自适应噪声底 + 起音多帧确认 + 尾点检测 + 最大时长保护 | `audio/vad.py`、`assets/silero_vad.onnx` | 单测：静音 / 语音 / 噪声样本判定正确；参数可配生效 | C-21、C-40 | T-1.1 |
| T-1.6 | P0 | 实现 AEC 与回采：NumPy NLMS 自适应滤波 + **`auto` 模式选择**（按 Profile `hardware_aec` 与探测结果自动在 hardware / software / none 间切换，A4、CON-13） | `audio/aec.py` | `auto` 在两种机型 Profile 下选择正确；software 模式回声衰减 ≥ 15dB | C-169、C-170、C-171 | T-1.3、B-03 |
| T-1.7 | P0 | 音频采集与播放拆分为独立并发任务，验证播放不阻塞采集 | `orchestrator` 中的音频任务编排 | 播报 30s 期间 ASR 仍持续产出 partial | C-01、C-02 | T-1.2、T-1.3、T-1.5 |
| T-1.8 | P1 | 实现方案 B 的桥接驱动与宿主机桥接节点（`AudioTransport` 可切换） | `audio/ros2_bridge.py`、`deploy/Dockerfile.bridge` | 切换 `audio.transport` 后功能等价 | C-50 | T-1.2、T-1.3 |
| T-1.9 | P1 | 实现音频诊断能力：采集活性探测、回环测试 | `observability/diagnostics.py` | `cli audio probe` / `cli audio loopback` 可用 | C-118、C-119、C-123 | T-1.7 |

---

## Phase 1A — 设备抽象层与多机型适配（A1/A2 已确认）

> 各机型 ROS2 麦克风采集与扬声器播报接口不同（A1/A2）。本阶段在音频链路之上加一层 `AudioDeviceAdapter`，使机型差异只存在于 Profile 与适配器实现中（FR-16、CON-11、design §3.6）。**必须先于实机联调完成**，否则每接一个机型都要改业务代码。

| ID | 优先级 | 任务 | 产出物 | 验证 | 对应验收 | 依赖 |
| --- | --- | --- | --- | --- | --- | --- |
| T-1A.1 | P0 | 定义设备抽象：`AudioCaptureContract` / `AudioPlaybackContract` / `AudioFrame`（含 `ts_device` / `seq`）+ 适配器注册表 `device_adapters` | `audio/device.py`、`audio/adapters/__init__.py` | 类型检查通过；业务层可仅依赖契约实例 | C-160 | T-0.4 |
| T-1A.2 | P0 | 实现机型 Profile 加载与校验：schema、必填项、`ros_distro` 与镜像标签匹配、能力声明一致性；自动探测失败时按 `audio.fallback_profile` 生效并 WARN，无 fallback 则**拒绝启动** | `config/devices/`、`audio/profile.py`、`config/schema.py` | 单测：合法 Profile 通过；探测失败且有 fallback 时启动成功并 WARN；未知机型且无 fallback / 缺字段 / 版本不匹配均报明确错误 | C-161、C-165 | T-1A.1、T-0.3 |
| T-1A.3 | P0 | 实现通用 ROS2 音频适配器（`base_ros_audio.py`）：按 Profile 绑定 topic / service、字段映射、QoS、采样率与声道转换（抗混叠低通重采样）、时间戳归一 | `audio/adapters/base_ros_audio.py` | 用两种不同采样率 / 消息结构的模拟 Profile 跑通采集与播放 | C-162、C-164 | T-1A.2、T-1.2、T-1.3 |
| T-1A.4 | P0 | 实现播放 `flush()` 的 Profile 化策略（service / topic_msg / stream_stop / volume_to_zero）；端到端打断预算在 T-4.3 联调确认 | `audio/adapters/base_ros_audio.py` | flush 到静音 ≤ 100ms；打断端到端仍 ≤ 300ms | C-163 | T-1A.3 |
| T-1A.5 | P0 | 建立适配层契约测试套件 `tests/contract/audio/`（帧率、时间戳、播放出声、flush、重采样质量、断线重连、能力一致性） | `tests/contract/audio/` | 套件可独立运行，作为新机型准入闸门；重采样 1kHz 正弦 THD+N ≤ 1%，禁止最近邻 / 直接抽取 | C-162、C-163、C-164、C-167 | T-1A.3、T-1A.4 |
| T-1A.6 | P1 | 设备诊断与运维命令：`cli audio devices`、`cli audio profile-test --profile <name>`（自检第 16 项在 T-7.5 接入） | `cli.py`、`observability/diagnostics.py` | 命令输出 Profile 列表、探测结果与机型匹配结论 | C-168 | T-1A.2、T-2.6 |
| T-1A.7 | P0 | 首批机型落地（**机型清单已确认，2026-11-01**）：为智元灵犀 X2 Pro 探索版（`agibot_x2_pro`）、智元灵犀 X2 Ultra 旗舰版（`agibot_x2_ultra`）、智元 A2（`agibot_a2`）、智元 A3（`agibot_a3`）、宇树 G1（`unitree_g1`）各编写 `config/devices/<model>.yaml` 与所需适配器，按 D12 清单逐机型填入接口契约并补齐契约测试 | `config/devices/*.yaml`、`audio/adapters/<model>.py` | 5 款 Profile 各自加载成功且契约测试通过；每款各完成一轮真实语音对话（FR-16.12） | C-161、C-167、C-177 | T-1A.5、D12、A11 |

---

## Phase 2 — 模型 Provider 驱动

| ID | 优先级 | 任务 | 产出物 | 验证 | 对应验收 | 依赖 |
| --- | --- | --- | --- | --- | --- | --- |
| T-2.1 | P0 | ASR 驱动：本地 FunASR 流式（`2pass`，partial + final，热词支持），通过 HTTP / WS 访问独立服务 | `providers/asr/funasr_local.py` | 喂测试音频 partial / final 正确；partial 延迟 ≤ 300ms | C-40、C-52 | T-0.5、T-2.7 |
| T-2.2 | P0 | ASR 驱动：云端流式（DashScope / OpenAI 兼容） | `providers/asr/dashscope_stream.py` | 同一段音频两驱动结果一致；可配热词 | C-52、C-58 | T-2.1 |
| T-2.3 | P0 | LLM 驱动：OpenAI 兼容统一驱动（覆盖 vLLM / Ollama / 云端），流式 + tool call | `providers/llm/openai_compat.py` | 首 token 达标；tool call 解析正确 | C-41、C-80 | T-0.5 |
| T-2.4 | P0 | TTS 驱动：本地 Qwen3-TTS 流式，音色 / 采样率可配 | `providers/tts/qwen3_tts.py` | 首音频 ≤ 400ms；音色切换生效 | C-42 | T-1.3 |
| T-2.5 | P0 | TTS 驱动：云端 TTS | `providers/tts/dashscope_tts.py` | 与本地驱动输出格式一致，可互换 | C-52 | T-2.4 |
| T-2.6 | P0 | 健康检查 + 熔断降级：失败计数、冷却、fallback 切换、自动回切（CLOSED / OPEN / HALF_OPEN） | `providers/registry.py`（resilience） | 停本地服务 → 熔断切 fallback；恢复后自动回切 | C-55、C-56 | T-2.1 ~ T-2.5 |
| T-2.7 | P0 | 实现 provider 预热与连接池，规避模型冷启动（ADR-04） | `providers/registry.py`（warmup / pool） | 启动日志可见预热完成；首轮延迟无劣化 | C-59 | T-2.1 ~ T-2.5 |
| T-2.8 | P0 | 本体语音模型容器化编排（拓扑 1）：`docker-compose.models.yml`（FunASR + TTS；LLM / KB 在远端，A5） | `deploy/docker-compose.models.yml` | 两个本体服务可独立起停；端口仅绑 `127.0.0.1` | C-133、C-173 | T-0.7 |
| T-2.9 | P1 | 编排层按 capabilities 自动降级功能（无 tool call 则关闭任务指令） | `core/orchestrator.py` | 配置不支持 tools 的驱动，工具能力被禁用并告警 | C-58 | T-0.4、T-2.3 |
| T-2.10 | P1 | 离线检测：启动探测公网可达性，不可达时锁定本地驱动 | `providers/registry.py` | 断网后云端驱动被锁定并告警 | C-134 | T-2.6 |
| T-2.11 | P0 | **算力分层落地（A5）**：LLM 驱动默认指向边端 / 云端（`LLM_BASE_URL`）；对「本机 LLM 地址」做白名单校验——默认拒绝，仅在显式开启 `allow_local_llm`（拓扑 2）且资源校验通过时放行；按位置分档设置超时 | `providers/llm/openai_compat.py`、`config/` | 仅改 `base_url` 即可在边端 / 云端切换；默认本体无 LLM 进程；显式开启拓扑 2 时不误拒绝 | C-172、C-174 | T-2.3、D14 |
| T-2.12 | P1 | 知识库检索驱动 `KnowledgeRetriever` + `http_rag`（`KB_BASE_URL`，超时 200ms 即跳过），支持 `none` 关闭 | `providers/knowledge/http_rag.py`、`dialog/rag.py` | 命中可返回；超时 / 5xx 正常对话并计数 `rag_skipped` | C-175、C-176 | T-2.11、D14 |

---

## Phase 2A — 多语种与方言（A8 已确认）

> 语言是数据不是分支：本阶段只扩展能力声明、路由与音色映射，不改状态机。
> 设计依据：design §7.6、ADR-13；需求 FR-15；验收 C-150 ~ C-158。

| ID | 优先级 | 任务 | 产出物 | 验证 | 对应验收 | 依赖 |
| --- | --- | --- | --- | --- | --- | --- |
| T-2A.1 | P0 | 在 `ASRCapabilities` / `TTSCapabilities` 增加 `languages` 声明并落实能力协商 | `providers/base.py` | 驱动可声明支持语言；编排层能读取并校验 | C-151、C-154 | T-2.1 ~ T-2.5 |
| T-2A.2 | P0 | 实现语言路由 `LanguageRouter`：`fixed` / `auto`、防抖（`min_chars` / `switch_confirm_turns`）、按语言选驱动、缺失降级 `zh` 并告警；同一驱动内切换不重建热流，跨驱动切换在防抖判定期间预连接目标驱动（FR-15.4） | `dialog/language.py` | 单测覆盖 fixed、auto、降级、防抖；降级有 WARN 与指标；跨驱动切换首响不劣于 NFR-1 | C-153、C-154、C-155、C-157 | T-2A.1 |
| T-2A.3 | P0 | 方言 ASR 驱动：外置 FunASR 方言模型接入（一语言一实例或合并），并支持云端方言驱动 | `providers/asr/funasr_dialect.py`、`dashscope_stream.py` | 5 种方言各 10 句识别，成功率按 D11 约定达标 | C-153 | T-2A.1、T-2.2 |
| T-2A.4 | P0 | 英文能力：ASR 英文识别、TTS 英文音色、提示词语言约束 | `providers/`、`dialog/prompt.py` | 英文多轮对话可用；回应为英文（C-151） | C-151 | T-2A.1、T-2.3 |
| T-2A.5 | P0 | 中英混说：整句送 ASR 不切段；`lang` 按字符占比判定；提示词禁止翻译专名 | `dialog/language.py`、`dialog/prompt.py` | 混说指令可正确执行 | C-152 | T-2A.2、T-2A.4 |
| T-2A.6 | P1 | 音色映射与降级标记：`language.tts_voices` 配置、无方言音色时普通话音色 + UI 标记 | `config/providers.yaml`、`core/orchestrator.py`、`webui/` | 方言提问以普通话播报且 UI 可见降级 | C-156 | T-2A.4 |
| T-2A.7 | P1 | 语言开关与可观测：`/api/languages`、WS `language.changed`、分语言指标与 WebUI 语言选择 | `api/`、`observability/metrics.py`、`webui/` | 语言可切换；四项语言指标可见 | C-157、C-158 | T-2A.2、T-6.* |
| T-2A.8 | P0 | 语言降级的会话标记与运行时故障处理：`lang` / `tts_lang` / `tts_fallback` 三字段写入 Turn 与记忆；降级方言播报时提示词强制普通话书面语；方言 ASR 服务不可达退回 `zh` 并 WARN + 熔断（FR-15.13 ~ FR-15.15） | `dialog/language.py`、`dialog/prompt.py`、`providers/registry.py` | 降级后 `lang` 保留、`tts_lang=zh`、`tts_fallback=true` 且提示词已切普通话；停方言服务后 30s 内退回 `zh` 并有告警 | C-154、C-156 | T-2A.2、T-2A.6、T-2.6 |

---

## Phase 1B — 意图识别与路由（R14 新增）

> 编号沿用 requirements.md A12 的 `T-1B.*`。本阶段**不新增状态机状态与事件**：意图只作为 ASR final 之后的路由决策（design §9.6、ADR-18）。
> 实施顺序：规则快速路径（T-1B.2）可先落地；语义分类依赖 Phase 2 的 LLM 驱动（T-2.3 / T-2.6），管线接入需 Phase 3 编排器就绪（T-3.6）。
> `command` 的完整校验（白名单 / 参数 / 二次确认）与 `confirm` / `cancel` 窗口依赖 Phase 5 工具策略（T-5.5、T-5.10）：本阶段先交付规则、分类、路由与兜底，确认窗口的场景验收在 Phase 8 完成（C-182）。
> 设计依据：design §9.6、§7.1、§7.3；需求 FR-17、NFR-1；验收 C-178 ~ C-186。
>
> **实施状态（2026-11-01）**：
> - T-1B.1 ~ T-1B.6 **已完成**：`dialog/intent.py`（IntentRuleMatcher / IntentRouter / route 兜底）、`providers/intent/llm_intent.py`、`config/intent.yaml` + schema 校验与热更新、orchestrator 快速路径与并行分类接入（stop 复用 barge-in、command 首音频前经 gate 转工具、晚到记 `intent_late_route_count`）、metrics 六项指标、session 五字段落库。
> - T-1B.7 **后端部分已完成**：`GET /api/intents`、`POST /api/intents/test`、`cli intent list` / `cli intent test`、自检第 19 项（intent）；**WebUI 面板（会话意图展示 / 配置页阈值 / 监控分布）随 Phase 6 交付**。
> - T-1B.8（P2 灰区澄清）**暂缓**，按计划留待后续迭代。
> - 单测 `tests/unit/test_intent.py`（11 例：快速路径 P95 ≤ 20ms / 不误判、route 窗口规则、低置信度与超时兜底、schema 拒非法配置、command 转工具与未知工具拒绝、晚到降级、落库读回、stop 语义打断）。

| ID | 优先级 | 任务 | 产出物 | 验证 | 对应验收 | 依赖 |
| --- | --- | --- | --- | --- | --- | --- |
| T-1B.1 | P0 | 意图契约与配置外置：定义 `IntentRequest` / `IntentResult`（`intent` / `confidence` / `slots` / `source` / `latency_ms`）与 `IntentProvider` 抽象 + `IntentCapabilities`；落地 `config/intent.yaml`（意图集合与示例话术、`confidence_threshold` 默认 0.6、`classify_timeout_ms` 默认 200、`active` 驱动），纳入 schema 校验与热更新 | `providers/base.py`、`config/intent.yaml`、`config/schema.py` | 代码中无硬编码意图枚举；新增意图仅改配置；非法配置被拒 | C-178、C-185 | T-0.3、T-0.4 |
| T-1B.2 | P0 | 规则快速路径：`stop` / `confirm` / `cancel` 的规则匹配（关键词 / 正则 + 语种），串行前置执行，判定 ≤ 20ms | `dialog/intent.py` | 单测：三类意图判定延迟 ≤ 20ms（P95）；非命中样本不误判 | C-181、C-182 | T-1B.1 |
| T-1B.3 | P0 | 语义分类驱动 `llm_intent`：LLM 结构化输出（JSON schema 约束意图与槽位），与 LLM / RAG 请求并行发起；200ms 超时、失败 WARN + 指标、接入熔断降级 | `providers/intent/llm_intent.py` | 桩服务可返回意图与槽位；超时按 `chat` 兜底并计数；停服务后熔断且对话不中断 | C-179、C-184 | T-1B.1、T-2.3、T-2.6 |
| T-1B.4 | P0 | 路由与兜底：低于阈值一律 `chat`；`command` 须高置信度产出工具名与参数并交工具白名单 / 参数校验 / 二次确认；`knowledge` 触发 RAG；`confirm` / `cancel` 仅 FR-9.10 确认窗口内生效、窗口外按 `chat`；`stop` 复用 Barge-in 取消路径 | `dialog/intent.py`、`core/orchestrator.py` | 单测覆盖全部路由分支；低置信度不触发工具；窗口外 `confirm` / `cancel` 按 `chat` | C-179、C-180、C-181、C-182 | T-1B.2、T-1B.3、T-5.10 |
| T-1B.5 | P0 | 管线接入：ASR final → 意图识别与路由 → `chat` / `knowledge` / `command` 三分支，不新增状态与事件；`command` 须在首块音频入队前生效，晚到则按 `chat` 完成本轮并记 `intent_late_route_count` | `core/orchestrator.py` | e2e：三类意图各走对链路；晚到的 `command` 不追溯执行 | C-183、C-184 | T-1B.4、T-3.6 |
| T-1B.6 | P0 | 意图可观测与落库：Turn 持久化 `intent` / `intent_confidence` / `intent_source` / `intent_slots` / `intent_latency_ms`；指标 `intent_distribution`、`intent_low_confidence_ratio`、`intent_fallback_count`、`intent_latency_ms`（按 `rule` / `classify` 路径）、`intent_late_route_count` | `dialog/session.py`、`observability/metrics.py` | 轮次接口可见意图字段；五项指标可查 | C-186 | T-1B.5、T-3.8、T-0.6 |
| T-1B.7 | P0 | WebUI 与运维接入：WS `intent.detected`、会话面板展示意图 / 置信度 / 来源 / 槽位、模型配置页意图集合与阈值、监控页意图分布与低置信度占比、`GET /api/intents` 与 `POST /api/intents/test`、`cli intent list` / `cli intent test` | `api/`、`webui/`、`cli.py` | 界面与命令可用；改阈值热更新立即生效 | C-185、C-186 | T-1B.6、T-6.*、T-7.5 |
| T-1B.8 | P2 | （FR-17.12）灰区澄清：置信度落在 `threshold` ~ `threshold + 0.15` 时先播报一句澄清并重试，最多一次 | `dialog/intent.py` | 灰区样本触发恰一次澄清；不出现连续追问 | C-180 | T-1B.4 |

---

## Phase 3 — 状态机与会话编排（核心）

| ID | 优先级 | 任务 | 产出物 | 验证 | 对应验收 | 依赖 |
| --- | --- | --- | --- | --- | --- | --- |
| T-3.1 | P0 | 定义事件模型与有界异步事件总线（单写者串行化、类型化事件、丢弃计数） | `core/events.py` | 单测：队列超限丢弃并计数；事件订阅分发正确 | C-28 | T-0.6 |
| T-3.2 | P0 | 实现 Turn 管理：`turn_id` 单调递增、当前有效 turn 维护、陈旧事件判定与丢弃 | `core/turn.py` | 单测：旧 turn_id 事件被丢弃并计数 | C-24 | T-3.1 |
| T-3.3 | P0 | 实现层级取消令牌：父子传播、`cancel()` 幂等、可 await 收敛 | `core/cancel.py` | 单测：父取消传播到全部子；重复取消不报错 | C-25 | T-0.1 |
| T-3.4 | P0 | 实现声明式状态机：转移表 + Guard + 动作钩子 + 启动时完整性校验 + 转移审计日志（`IDLE` / 常开聆听 `ACTIVE_IDLE` 显式声明「常驻无超时」并交由全局心跳兜底，FR-7.2） | `core/state_machine.py` | 单测覆盖全部转移；非法事件忽略且告警；完整性校验能拦截缺超时且未显式声明常驻无超时的状态 | C-20、C-21、C-23 | T-3.1、T-3.2 |
| T-3.5 | P0 | 实现看门狗：每状态超时注册与触发、全局 1s 心跳检测无进展、自恢复流程；「进展」判据为音频帧到达、VAD / KWS 推理产出、事件被处理、provider 健康探测返回任一即算（`IDLE` 与常开聆听 `ACTIVE_IDLE` 不得因无对话事件被判无进展）；恢复时只取消当前轮次对话链路（LLM 生成 / TTS 合成 / 播放队列），已移交执行层的异步任务不取消（FR-7.7） | `core/watchdog.py` | 单测：挂起协程后 1s 内触发恢复；长任务执行中触发看门狗后异步任务仍继续并保留结果；待机态与常开聆听态静置 10 分钟仅有音频帧 / 推理产出时不误触发恢复 | C-22、C-27 | T-3.4 |
| T-3.6 | P0 | 实现全双工会话编排器：音频 → VAD → ASR → 记忆 → LLM → TTS → 播放 全链路串接，广播事件到 WebUI | `core/orchestrator.py` | 本地文件音频模式下跑通完整一轮对话 | C-02、C-90、C-92 | T-1.*、T-2.*、T-3.4、T-3.5 |
| T-3.7 | P0 | 实现 LLM 流式按句切分 → 逐句送 TTS 流式合成 → 播放队列（边说边生成） | `core/orchestrator.py` | 长回复逐句流出播放，非等待整段 | C-44、C-45 | T-3.6 |
| T-3.8 | P0 | 落库轮次与事件日志（含 timings），支持会话回放导出 | `dialog/session.py`、`observability/` | `/api/logs/events` 可导出并重放 | C-104 | T-3.6 |
| T-3.9 | P0 | 实现手动强制复位能力（`EV_RESET`） | `core/state_machine.py`、`api/` | `cli session reset` 可将异常会话复位 | C-30 | T-3.5 |
| T-3.10 | P1 | 优雅启停：停止信号后完成当前轮次并释放音频与模型连接 | `main.py` 生命周期 | 无残留连接与进程 | C-29 | T-3.6 |

---

## Phase 3A — 唤醒词与待机唤醒（B-06 已确认）

> 与 Phase 3 并行推进。核心原则：**KWS 只在待机态运行**，活跃期关闭，保证唤醒不进入打断链路（INV-8 / INV-9）。
> 设计依据：design §5.6、§7.1、ADR-12；需求 FR-14；验收 C-140 ~ C-148。

| ID | 优先级 | 任务 | 产出物 | 验证 | 对应验收 | 依赖 |
| --- | --- | --- | --- | --- | --- | --- |
| T-3A.1 | P0 | 定义 `WakeWordProvider` / `KwsStream` 抽象与 `KwsCapabilities`，接入 provider 注册表 | `providers/base.py`、`providers/kws/` | 注册与解析可用；capabilities 声明帧长与采样率 | C-141 | T-0.5、T-2.2 |
| T-3A.2 | P0 | 实现 openWakeWord ONNX 驱动：模型加载、`warmup`、逐帧推理、输出 `score` 与多唤醒词模型并行 | `providers/kws/openwakeword.py` | 喂入样音频可得 score；模型路径可配多份 | C-140、C-141 | T-3A.1 |
| T-3A.3 | P0 | 实现待机态 KWS 调度：按状态启停、帧对齐、阈值判定、命中冷却；待机态不启 ASR 会话 | `audio/kws.py` | 待机仅跑 KWS；进入活跃态 KWS 停止、ASR 启动 | C-144、C-148 | T-3A.2、T-3.4 |
| T-3A.4 | P0 | 状态机接入唤醒闭环：`IDLE → WAKING → ACTIVE_IDLE`，活跃窗口计时与每轮刷新，超时回 `IDLE` 并释放 ASR；`wakeword.enabled=false` 时不启用窗口超时，常驻 `ACTIVE_IDLE` 且 ASR 保持可用（FR-14.8） | `core/state_machine.py`、`core/orchestrator.py` | 单测覆盖新增转移；超时后资源确实释放；关闭唤醒词后静置超过窗口仍保持 `ACTIVE_IDLE` 且 ASR 可用 | C-143、C-144、C-147 | T-3A.3、T-3.4 |
| T-3A.5 | P0 | 唤醒反馈与 ASR 预连接：`feedback`（tts / beep / none）+ `preopen_asr`，唤醒到反馈 ≤ 500ms | `core/orchestrator.py` | 唤醒有可感知反馈；首句识别无可感知额外延迟 | C-140、C-142 | T-3A.4、T-2.1 |
| T-3A.6 | P0 | 防误唤醒：阈值 / 冷却 / 二次确认三项配置化，并埋点 `wake_*` 指标 | `config/providers.yaml`、`observability/metrics.py` | 三项参数生效；误唤醒率达标；四项指标可见 | C-145、C-148 | T-3A.3 |
| T-3A.7 | P1 | 配置与资产落地：`wakeword` 配置段、`assets/wakeword/` 模型（**中英双语各一**，FR-14.14）与提示音、镜像打包；`enabled=false` 退化为常开聆听，并做跨字段校验：默认本地 ASR，云端 ASR 须置 `open_ear.cloud_asr_ack=true` 否则配置校验 FAIL（FR-14.8） | `config/`、`config/schema.py`、`assets/`、`deploy/Dockerfile` | 换唤醒词仅改配置；中英唤醒词均可用；关闭后启动即 `ACTIVE_IDLE`；云端 ASR + 未置 ack 时启动失败 | C-141、C-147 | T-3A.2、T-0.7、T-0.3 |
| T-3A.8 | P1 | 可观测与运维：`cli wake test` / `cli wake status`、自检第 13 项（含常开聆听 ASR 本地性校验与持续上传提示）、WebUI 唤醒指示与活跃窗口倒计时、指标埋点 `open_ear_cloud_audio_ms` | `cli.py`、`observability/diagnostics.py`、`observability/metrics.py`、`webui/` | 命令可用；自检输出唤醒项；WebUI 展示 `wake.detected`；常开聆听 + 云端 ASR 时上传时长指标可见 | C-148、C-147 | T-3A.6、T-6.* |
| T-3A.9 | P0 | 长任务与活跃窗口联动、待机态结果播报：任务发起时刷新窗口、执行期不刷新、播报完成后刷新；超窗口回 `IDLE` 但不取消任务；待机态结果默认缓存 + 提示音、唤醒后播报，`standby.announce=true` 时立即播报但不拉起 ASR、不支持 Barge-in（FR-14.15、FR-14.16）；关闭唤醒词时常开聆听不存在待机态，结果直接播报且支持 Barge-in，缓存与 `announce` 不适用（FR-14.8） | `core/orchestrator.py`、`core/state_machine.py`、`config/providers.yaml` | 构造 300s 长任务：执行中回 `IDLE` 且任务未取消；待机态结果有提示音且唤醒后播报；开启 `announce` 后播报期间不拉起 ASR；关闭唤醒词时结果直接播报且可打断 | C-144、C-147 | T-3A.4、T-3.6 |

---

## Phase 4 — Barge-in 打断机制

| ID | 优先级 | 任务 | 产出物 | 验证 | 对应验收 | 依赖 |
| --- | --- | --- | --- | --- | --- | --- |
| T-4.1 | P0 | 实现打断检测器：多帧能量确认 + 播放期门限提升 + 保护窗口（默认随 AEC 模式自适应 100 / 200 / 300ms） | `audio/barge_in.py` | 单测：噪声不触发、真实语音触发；参数生效 | C-02、C-03、C-169 | T-1.5、T-1.6 |
| T-4.2 | P0 | 实现语义回声剔除：ASR partial 与最近播报文本相似度比对过滤 | `audio/barge_in.py` | 自播音频被识别时判为回声不触发打断 | C-07 | T-4.1、T-2.1 |
| T-4.3 | P0 | 实现打断执行序列：停播 → 递增 turn_id → 取消 LLM / TTS → 清音频队列 → 归档轮次 → 通知 UI → 回 LISTENING，含各阶段耗时埋点 | `core/orchestrator.py`（`INTERRUPTING`） | 总耗时 ≤ 300ms（P95）；各阶段耗时可见 | C-03、C-05、C-06 | T-4.1、T-3.3、T-3.4 |
| T-4.4 | P0 | 打断后复用 ASR 热会话，不重建连接 | `core/orchestrator.py` | 打断后二次识别延迟显著低于首次 | C-04 | T-4.3、T-2.1 |
| T-4.5 | P0 | 打断时归档轮次：标记 `interrupted`，**仅已送入播放队列的助手文本片段** + `spoken_ratio` 写入记忆，未播出内容只进事件日志（FR-6.8） | `dialog/memory.py` | 轮次记录正确；打断后追问可被理解；未播出内容不在记忆 | C-09、C-72 | T-4.3 |
| T-4.6 | P0 | 打断参数配置化并支持运行时热更新（门限 / 帧数 / 保护窗口 / 语义开关 / 模式） | `config/app.yaml` `barge_in` 段 + 热更新通道 | 改参数立即生效，无需重启 | C-10、C-57 | T-0.3、T-4.1 |
| T-4.7 | P1 | 回归验证：连续 20 次打断不错乱 | e2e 用例 | 每次回应对应最新话语 | C-08 | T-4.3 |
| T-4.8 | P1 | 支持 `barge_in.mode=disabled` 半双工兜底 | 检测器旁路 | 高噪环境下不误打断 | C-11 | T-4.1 |
| T-4.9 | P0 | AEC 模式回归：有硬件 / 无硬件两种 Profile 下验证 `auto` 选择、回声衰减、播报期不误打断（A4） | `tests/contract/audio/aec_*` | 两种机型均通过；模式切换可见 | C-169、C-170 | T-1.6、T-1A.5 |

---

## Phase 5 — 对话记忆与任务指令

| ID | 优先级 | 任务 | 产出物 | 验证 | 对应验收 | 依赖 |
| --- | --- | --- | --- | --- | --- | --- |
| T-5.1 | P0 | 实现会话记忆存储（SQLite 驱动，异步串行写）+ 消息模型 + 读写接口 | `dialog/memory.py`、`dialog/session.py` | 单测：增删查正确；容器重启后历史可恢复 | C-70、C-126 | T-3.6 |
| T-5.2 | P0 | 实现上下文组装、token 预算滑窗裁剪与提示词模板化 | `dialog/prompt.py` | 100 轮对话不溢出上下文 | C-70、C-71 | T-5.1 |
| T-5.3 | P1 | 实现滚动摘要记忆：超窗历史压缩为摘要保留 | `dialog/memory.py`（summary） | 超长对话可引用早期内容 | C-73 | T-5.2 |
| T-5.4 | P0 | 实现工具注册表与定义模型（名称 / 描述 / 参数 schema / 风险等级 / 超时 / 适配器） | `tools/base.py`、`tools/registry.py` | `/api/tools` 返回工具列表含风险等级 | C-80 | T-0.4 |
| T-5.5 | P0 | 接入 LLM Function Calling：解析 tool_call → 派发 → 结果回灌 LLM | `core/orchestrator.py`（`EXECUTING`） | 说「去 A 点」产出正确 tool_call | C-80、C-82 | T-2.3、T-5.4 |
| T-5.6 | P0 | 实现 ROS2 执行适配器（service / action） | `tools/adapters/ros2_adapter.py` | 对接真实 ROS2 服务执行成功 | C-81 | T-5.4、B-05 |
| T-5.7 | P2 | **（A6 推迟）** 仅保留 REST / MQTT 执行适配器的扩展点与配置骨架，不实现、不验收；待 IoT 具体需求明确后再排期 | `tools/adapters/rest_adapter.py`、`mqtt_adapter.py`（骨架） | 适配器注册机制可容纳新实现，业务代码零改动 | C-87 | T-5.6 |
| T-5.8 | P0 | 内置工具集：播报、机器人导航、机器人 IO 控制（基于 `config/tools.yaml` 映射）；不含 IoT 设备控制（A6） | `tools/builtin/*.py`、`config/tools.yaml` | 至少 3 个工具可语音触发执行 | C-81 | T-5.6 |
| T-5.9 | P0 | 异步任务执行：长任务不阻塞对话、进度可播报、结果回灌 | `tools` + `orchestrator` | 长任务期间追问被正常响应 | C-83 | T-5.5 |
| T-5.10 | P0 | 安全策略：白名单 / 黑名单 + 参数校验 + 高风险二次确认 + 调用限流；二次确认交互按 FR-9.10：确认播报期间不启用 Barge-in，语音交给规则 / LLM 判定确认或取消（不新建轮次），`tools.confirm_timeout_ms`（默认 10s）超时或无法判定按取消并播报 | `tools/policy.py` | 越界参数被拦截；移动类指令需确认；确认播报不被语音打断，说「确认」执行、说「取消」/超时不执行并播报 | C-84、C-85 | T-5.4 |
| T-5.11 | P1 | 工具超时与取消 | `tools/registry.py` | 桩不返回的工具被超时中止并播报失败 | C-86 | T-5.9 |
| T-5.12 | P0 | 知识片段注入提示词：预算控制、来源边界标注、命中记录（`doc_id` / `score` / `skipped`）写入轮次 `rag_hits` 字段 | `dialog/prompt.py`、`dialog/rag.py` | 回答基于知识且每轮命中可追溯；片段内指令不被执行 | C-175、C-176 | T-2.12、T-5.2 |
| T-5.13 | P1 | WebUI 展示本轮知识命中来源（可选展开） | `webui/views/…` | 运维可看到引用了哪些片段 | C-176 | T-5.12、T-6.5 |

---

## Phase 6 — API 与 WebUI 控制台

| ID | 优先级 | 任务 | 产出物 | 验证 | 对应验收 | 依赖 |
| --- | --- | --- | --- | --- | --- | --- |
| T-6.1 | P0 | 实现 WebSocket 事件网关：协议版本化、事件推送、客户端指令接收 | `api/ws.py`、`api/schemas.py` | 客户端可收到 state / asr / llm / barge_in 事件 | C-90 | T-3.6 |
| T-6.2 | P0 | 实现 REST 接口全集（会话 / 配置 / 驱动 / 工具 / 指标 / 日志 / 诊断） | `api/rest.py` | 接口按 design §10.3 返回正确 | C-90、C-124 | T-0.3、T-3.8 |
| T-6.3 | P0 | 实现配置热更新与回滚（校验 + 预检 + 原子生效 + rollback） | `api/rest.py`、`config/loader.py` | 非法配置被拒；回滚可用 | C-57、C-94 | T-6.2、T-2.6 |
| T-6.4 | P0 | 实现口令鉴权（写操作需认证） | `api/auth.py` | 未认证写请求被拒 | C-98 | T-6.2 |
| T-6.5 | P0 | 搭建 WebUI 工程（Vue3 + TS + Vite）+ REST / WS 客户端封装 + 构建接入镜像 | `webui/` | 本地可启动并连通后端；构建产物被镜像包含 | C-90、C-111 | T-6.1、T-6.2 |
| T-6.6 | P0 | 实时会话面板：状态机可视化、音频活动指示、ASR 转写流、LLM 流式文本、播报状态、打断时间线 | `webui/src/views/SessionView.vue` | 对话时各项实时刷新 | C-90、C-91、C-93 | T-6.5 |
| T-6.7 | P0 | 运行控制：开始 / 结束会话、手动打断、麦克风静音、文本输入；待机态（含开启唤醒词时的 `IDLE`）开始会话与文本输入视作显式唤醒，发 `EV_SESSION_START` / `EV_TEXT_INPUT` 直接激活并开启活跃窗口，不要求唤醒词（FR-11.2） | `webui/src/components/Controls.vue`、`core/state_machine.py` | 文本输入可跑通全链路；待机态点击开始或打字后直接进入 `THINKING` / `ACTIVE_IDLE`，无需唤醒词 | C-92、C-93 | T-6.6 |
| T-6.8 | P0 | 模型配置页：驱动查看与切换、参数编辑、连通性测试、应用与回滚 | `webui/src/views/ConfigView.vue` | 热切换生效且不中断会话 | C-53、C-94、C-95 | T-6.3、T-6.5 |
| T-6.9 | P1 | 工具页与记忆管理页 | `ToolsView.vue`、`MemoryView.vue` | 工具列表 / 调用历史 / 记忆查看清除可用 | C-74 | T-6.5 |
| T-6.10 | P1 | 监控页：延迟分位数、打断成功率、错误率、状态流转日志 | `MetricsView.vue` | 指标可视化 | C-46、C-96 | T-0.6、T-6.5 |
| T-6.11 | P1 | 运维诊断页：一键自检、音频回环、provider 测试、强制复位、日志导出 | `DiagnosticsView.vue` | 各诊断动作可从界面触发 | C-99、C-123 | T-6.5、T-1.9 |
| T-6.12 | P1 | 局域网访问与响应式适配（PC / 平板） | 构建与样式适配 | 平板可正常访问操作 | C-97 | T-6.5 |

---

## Phase 7 — 容器化交付与运维

> 详细方案见 [deployment.md](./deployment.md)

| ID | 优先级 | 任务 | 产出物 | 验证 | 对应验收 | 依赖 |
| --- | --- | --- | --- | --- | --- | --- |
| T-7.1 | P0 | 完善应用镜像：多阶段构建、非 root 用户、清理缓存、体积收敛；**参数化 `ROS_DISTRO` 产出 humble / jazzy 双标签，CI 双构建** | `deploy/Dockerfile`、CI 配置 | 两个发行版镜像体积均达标；`docker exec whoami` 非 root；各自可启动 | C-111、C-114、C-115、C-166 | T-0.7、B-07、B-08 |
| T-7.2 | P0 | 完善 DDS 共存配置：`cyclonedds.xml`、Compose 环境变量、`/dev/shm`、`ipc: host` 全量核对 | `deploy/docker-compose.yml`、`cyclonedds.xml` | 容器与宿主机双向可见；换网 / 重启后仍可发现 | C-117、C-121、C-122 | T-0.8、B-09 |
| T-7.3 | P0 | 资源限制与环境隔离：CPU / 内存上限、线程数限制（`OMP_NUM_THREADS`）、安全加固（`cap_drop` / `no-new-privileges` / 只读配置挂载）；**按 D13 实测输出本体资源预算表（应用 + ASR + TTS，不含 LLM）** | `docker-compose.yml`、资源预算表 | 资源被限制在上限内；本体其他业务不受影响；预算表与实际 `docker stats` 一致 | C-115、C-127、C-173 | T-7.1、B-10、B-11 |
| T-7.4 | P0 | 一键部署脚本 `install.sh`：检测宿主机 `ROS_DISTRO` → 选择对应镜像标签 → 环境检查 → 生成 .env（含 `ROBOTCHAT_DEVICE_PROFILE`）→ 加载镜像 → 启动 → 等待就绪 → 自检 | `deploy/scripts/install.sh` | 干净环境单命令完成部署，5 分钟内就绪；发行版与机型 Profile 自动带入 | C-110、C-161、C-166 | T-7.1、T-7.2、T-7.3、T-1A.2 |
| T-7.5 | P0 | 启动自检 `selfcheck.sh` 与 `cli doctor`：**19 项检查**（含机型 Profile、LLM / KB 连通、AEC 模式、意图集合与分类驱动）+ PASS / WARN / FAIL 结论 + 可操作 hint | `deploy/scripts/selfcheck.sh`、`cli.py` | 自检输出结构化结论；故意制造故障（含 Profile 错误、LLM 不可达、意图配置非法）能被检出 | C-123、C-168、C-171、C-174、C-185 | T-1.9、T-1A.6、T-2.6、T-2.11、T-1B.1、T-7.4 |
| T-7.6 | P0 | 健康 / 就绪 / 存活三级探测端点与 Compose healthcheck | `api/rest.py` | 就绪前 503，就绪后 200 且含子系统明细 | C-124 | T-6.2 |
| T-7.7 | P0 | systemd 服务单元：开机自启、崩溃重启、与 Docker 依赖顺序 | `deploy/systemd/robotchat.service` | 重启机器人后自动启动并恢复就绪 | C-128 | T-7.4 |
| T-7.8 | P0 | 数据持久化与卷管理：数据卷 / 日志卷 / 配置只读挂载；容器重建数据不丢 | `docker-compose.yml` | `down` 后 `up`，配置与历史会话数据仍在 | C-126 | T-5.1、T-7.4 |
| T-7.9 | P0 | 崩溃自愈验证：`restart: unless-stopped` + healthcheck 联动 | Compose 配置 | `docker kill` 后自动拉起并恢复就绪 | C-125 | T-7.7 |
| T-7.10 | P1 | 日志轮转与保留策略（Docker json-file + 应用 JSONL 按天轮转） | 日志配置 | 长时间运行磁盘不被日志占满 | C-131 | T-7.3 |
| T-7.11 | P1 | 升级与回滚脚本 `upgrade.sh` / `rollback.sh`，含升级前备份 | `deploy/scripts/upgrade.sh`、`rollback.sh` | 演练通过，可恢复到上一版本 | C-129、C-130 | T-7.4 |
| T-7.12 | P1 | 备份与恢复脚本 `backup.sh` / `restore.sh`（配置 + 数据卷） | `deploy/scripts/backup.sh`、`restore.sh` | 备份可成功恢复 | C-126 | T-7.8 |
| T-7.13 | P1 | 诊断包导出 `cli diagnose export`（配置脱敏 + 日志 + 指标 + 自检结果） | `cli.py`、`observability/diagnostics.py` | 产出一个可交付的故障诊断包 | C-132 | T-7.5 |
| T-7.14 | P1 | 模型服务独立编排与故障隔离验证（拓扑 2） | `deploy/docker-compose.models.yml` | 重启模型容器，主服务不崩溃并自动恢复 | C-133 | T-2.8 |
| T-7.15 | P1 | 离线可用性验证：断网后本地链路正常、云端驱动自动锁定并告警 | 部署配置 | 断网场景可正常对话 | C-134 | T-2.10 |
| T-7.16 | P1 | 编写运维手册落库（命令速查、故障排查决策树、参数调优指引） | 并入 `deployment.md` | 现场人员可依此独立排障 | — | T-7.5 |
| T-7.17 | P2 | 多实例 / 高可用部署方案（消除升级停机窗口） | 方案文档 + 编排 | 升级期间服务不中断 | C-129 | T-7.11 |

---

## Phase 8 — 测试、调优与验收

| ID | 优先级 | 任务 | 产出物 | 验证 | 对应验收 | 依赖 |
| --- | --- | --- | --- | --- | --- | --- |
| T-8.1 | P0 | 单元测试：状态机全转移、取消传播、VAD、打断检测、陈旧事件丢弃、记忆裁剪、配置校验、唤醒调度与活跃窗口超时、语言路由与降级、意图规则快速路径与路由兜底 | `tests/unit/` | 覆盖率达标，全部通过 | C-101、C-21 ~ C-25、C-144、C-154、C-178 ~ C-182 | Phase 2A ~ 5 |
| T-8.2 | P0 | 端到端测试：完整对话、打断、插话、工具调用、降级恢复（本地文件音频 + 桩服务，无需硬件），含中英 / 方言用例与意图三条链路（chat / knowledge / command）及低置信度兜底用例 | `tests/e2e/` | e2e 套件全绿 | C-102、C-150 ~ C-155、C-179 ~ C-184 | T-6.1 |
| T-8.3 | P0 | 延迟基准测试与调优：测量各阶段延迟并迭代优化至目标值 | `tests/benchmark/` + 调优记录 | C-40 ~ C-43 全部达标 | C-40 ~ C-43 | T-8.2 |
| T-8.4 | P0 | 稳定性与长稳测试：72h 运行、随机打断、故障注入（杀模型进程 / 断 ROS2 / 重启容器） | 测试脚本 + 报告 | C-26、C-28、C-125、C-133 达标 | C-26、C-28 | T-8.2 |
| T-8.5 | P0 | 机器人本体联调：真实音频链路、打断、任务执行、容器化部署全流程验证；**必须覆盖 5 款目标机型（智元灵犀 X2 Pro 探索版 / X2 Ultra 旗舰版 / 智元 A2 / 智元 A3 / 宇树 G1），Humble 与 Jazzy 各至少一台** | 联调记录 | checklist 全部 P0 检查项通过（含 C-160 ~ C-166、C-177、C-178 ~ C-185） | 全部 P0 | T-1A.7、T-1B.5、T-7.4、T-8.4 |
| T-8.6 | P0 | 部署验收：双发行版镜像体积 / 无重依赖 / 无密钥 / ROS2 共存 / 资源限制 / 开机自启 / 机型 Profile 生效 / **AEC 模式与 LLM-KB 边界** | 验收报告 | C-110 ~ C-128、C-160 ~ C-166、C-169 ~ C-177 全部通过 | C-110 ~ C-128、C-160 ~ C-166、C-169 ~ C-177 | T-8.5 |
| T-8.7 | P1 | 现场参数调优：在真实环境完成 barge-in / VAD / AEC 参数标定，并按 D15 完成意图阈值与示例话术标定 | 标定记录 | 打断成功率与误打断率达到可接受水平；意图分类准确率达标且低置信度不误触发控制 | C-03、C-07、C-08、C-179、C-180 | T-8.5、D15 |
| T-8.8 | P1 | 机器人既有 ROS2 功能回归验证 | 回归报告 | 既有节点与功能无回归 | C-120 | T-8.5 |

---

## 关键路径与里程碑

```
M1 容器地基建好 + 骨架可跑通
     Phase 0（含容器化地基与 ROS2 共存打通）+ Phase 1（本地文件音频模式）
        ↓
M1.5 设备适配层就绪（换机型只换 Profile）
     Phase 1A（抽象接口 + Profile + 契约测试；首批 5 款机型：智元灵犀 X2 Pro / X2 Ultra / 智元 A2 / 智元 A3 / 宇树 G1，接口契约按 D12 采集）
        ↓
M2 一轮对话闭环
     Phase 2 + Phase 3
        ↓
M2.5 唤醒闭环可用
     Phase 3A（待机唤醒 → 活跃期 → 回到待机）
        ↓
M2.6 多语言可用（中英 + 方言）
     Phase 2A（能力声明 + 语言路由 + 音色映射）
        ↓
M2.7 意图规则与分类可用
     Phase 1B（规则快速路径 + 语义分类并行 + 低置信度兜底 chat；command 完整校验与确认窗口随 Phase 5 收敛）
        ↓
M3 秒打断达标（核心验收里程碑）
     Phase 4（≤300ms）
        ↓
M4 记忆与任务指令
     Phase 5
        ↓
M5 可运维可观测
     Phase 6
        ↓
M6 容器化交付与运维就绪
     Phase 7
        ↓
M7 工业级稳定性与部署验收
     Phase 8
```

---

## 前置阻塞项（实施前必须澄清）

| ID | 事项 | 阻塞任务 | 建议确认时机 |
| --- | --- | --- | --- |
| B-01 | ~~ROS2 麦克风采集 topic 名称与消息类型~~ **已确认（A1）：每机型不同**；机型清单已确认（A11，5 款） → 改为按 D12 清单逐机型采集接口契约并补 Profile 与适配器 | T-1.2、T-1A.7 | 实施首日（清单采集） |
| B-02 | ~~ROS2 扬声器播放方式（service / topic）与消息类型~~ **已确认（A2）：每机型不同** → 由 Profile 声明，适配器实现 | T-1.3、T-1A.* | 实施首日（清单采集） |
| B-05 | 已有机器人控制接口清单（ROS2 服务 / 动作）；IoT 控制接口随 A6 推迟 | T-5.6、T-5.8 | 实施首日 |
| B-09 | 宿主机使用的 RMW 实现（CycloneDDS / FastDDS） | T-0.8、T-7.2 | 实施首日（否则容器发现必然失败） |
| B-08 | ~~机器人 ROS2 发行版本~~ **已确认（A3）：Humble 与 Jazzy，均须支持** | T-0.2、T-0.7、T-7.1 | 已消解 |
| B-07 | ~~机器人本体 Docker / Compose 安装情况与版本~~ **已确认（A10）：已安装 Docker**；Compose 版本待核实 | T-7.1 | Phase 0 |
| B-03 | ~~是否存在硬件 AEC / 波束成形~~ **已确认（A4）：因机型而异，有则用硬件、无则软件，且可配置** | T-1.6、T-4.9 | 已消解 |
| B-04 | ~~本体算力（决定本地模型规模）~~ **已确认（A5）：本体跑 ASR / TTS / VAD / AEC / KWS，LLM / KB 在边端或云端** | T-2.11、T-7.3 | 已消解（实测余量见 B-11） |
| B-11 | 首批 5 款机型（A11）本体算力逐机型实测（CPU / 内存余量 / GPU 或 NPU、语音模型同时驻留占用） | T-2.11、T-7.3 | Phase 2 |
| B-12 | 边端 / 云端 LLM 与知识库契约（地址 / 模型 / 鉴权 / 检索接口） | T-2.11、T-2.12 | Phase 2 |
| B-13 | **业务意图清单与示例话术、`intent.confidence_threshold` 现场标定**（A12 默认 6 类，业务侧可能需扩展或调整判定范围） | T-1B.2、T-1B.4、T-1B.7 | Phase 2 后（LLM 可调用时） |
| B-10 | 机器人本体内存与磁盘余量 | T-7.3、T-2.8 | Phase 2 |
| B-06 | ~~是否需要唤醒词~~ **已确认：需要唤醒词** | Phase 3A（T-3A.*） | 已消解（2026-09） |

> **实施首日优先确认 B-01 / B-02 / B-05 / B-09**：这四项分别阻塞音频采集、音频播放、工具集与容器 DDS 连通，是最容易造成返工的前置条件。
