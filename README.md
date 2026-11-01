# RobotChat

机器人本体全双工语音交互系统：本地唤醒、实时打断、流式 ASR/LLM/TTS、工具调用、RAG 与多轮记忆，
配套 WebUI 控制台与容器化交付。需求、设计与验收标准见
[.trae/specs/full-duplex-voice-interaction](./.trae/specs/full-duplex-voice-interaction/)。

## 支持的机型

接口差异全部收敛在机型 Profile（`config/devices/<profile>.yaml`）与适配器中，
上层状态机、编排器与 VAD / ASR / LLM / TTS / KWS 代码不感知具体硬件（FR-16.7）。

| Profile | 适配器 | 接口栈 | 状态 |
| --- | --- | --- | --- |
| `agibot_x2_pro` / `agibot_x2_ultra` | `agibot_aimdk_msgs` | ROS2 话题 + `token_id` 续播 + 音频焦点 | 草稿，待现场实测 |
| `agibot_a2` / `agibot_a3` | `agibot_ros2_plugin` | `ros2_plugin_proto` 包 protobuf + HTTP RPC 音量 | 草稿，待现场实测 |
| `unitree_g1` | `unitree_g1_native` | UDP 组播裸 PCM + `unitree_sdk2py` DDS RPC | 草稿，待现场实测 |
| `generic_ros_audio` | `base_ros_audio` | 通用 ROS2 音频话题（默认） | 可用 |
| `sim_virtual_audio` | `base_ros_audio` | Docker 仿真（`std_msgs` 字节流） | 可用 |

带「待现场实测」的 Profile 中，无法从官方文档确认的字段已标注 `TODO-实测`
（X2 采集话题后缀与帧长、A2/A3 protobuf 字段、A3 音量接口、G1 组播包格式）。

## 功能

- 全双工对话：采集与播报独立任务，支持播报中插话打断（含语义回声剔除）
- 唤醒词：本地 ONNX 推理 + 活跃窗口 + 防误唤醒
- 语言：中文、英文、粤语及多种方言，支持中英混说与驱动降级
- 工具调用：Function Calling、异步任务、高风险二次确认、超时与取消
- 知识与记忆：RAG 命中注入、SQLite 多轮记忆、滚动摘要、Token 预算
- 可观测性与运维：结构化日志、18 项自检、三级健康探测、诊断包导出
- WebUI：会话、监控、工具、记忆、配置、诊断 6 个页面

## 目录结构

```
src/robotchat/
  audio/        缓冲、重采样、VAD、AEC、适配器（adapters/）与传输（文件 / ROS2）
  providers/    ASR / LLM / TTS / KWS / RAG 驱动与注册表（熔断、降级、预热）
  dialog/       会话、记忆、摘要、语言路由、RAG 注入
  tools/        工具注册表、策略、ROS2 执行适配器
  core/         事件总线、状态机、编排器、看门狗
  api/          WS 网关、REST、鉴权
config/         app.yaml、providers.yaml、tools.yaml 与 devices/ 机型 Profile
webui/          Vue 控制台
deploy/         Dockerfile、Compose（主 / 模型 / 仿真）、运维脚本
tests/          unit / contract / e2e
```

## 本地开发

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows；Linux / macOS 用 source .venv/bin/activate
pip install -e ".[dev]"
python -m robotchat.main        # 默认 http://127.0.0.1:8080
```

WebUI 源码在 `webui/`（`npm install && npm run build` 后由服务端挂载到 `/`）。

## 配置

- 默认模板：`config/app.yaml`、`config/providers.yaml`、`config/tools.yaml`
- 机型 Profile：`config/devices/*.yaml`，由 `audio.device_profile` 或 `ROBOTCHAT_DEVICE_PROFILE` 选择；
  开启 `auto_detect_profile` 后探测失败按 `fallback_profile` 兜底，为空则拒绝启动（FR-16.11）
- 多环境叠加：`ROBOTCHAT_ENV=dev|sim|robot` → 追加 `config/app.<env>.yaml` 深合并
- 环境变量覆盖：`ROBOTCHAT__` + 配置路径（`__` 分隔、大写），如 `ROBOTCHAT__AUDIO__AEC__MODE=software`
- 优先级：环境变量 > 运行时变更 > 现场配置 > 默认配置

## 测试

```bash
pytest -q                      # 全量 349 项
pytest tests/unit -q           # 单元
pytest tests/contract -q       # 适配层契约
pytest tests/e2e -q            # 端到端（全离线，无需硬件与网络）
ruff check src tools tests deploy
mypy src tools
```

e2e 覆盖完整对话与多轮记忆、20 次连续打断、工具调用与二次确认、熔断降级恢复、中/英/方言路由。

## CLI

```bash
robotchat doctor                    # 启动自检（18 项 PASS / WARN / FAIL）
robotchat audio devices             # 列出机型 Profile 与匹配结论
robotchat audio probe               # 采集活性探测
robotchat audio loopback            # 播放 / 采集回环
robotchat audio profile-test <名称>  # 单机型接口联通性验证
robotchat wake test <音频>           # 唤醒链路验证
robotchat wake status               # 唤醒状态与活跃窗口
robotchat session reset             # 强制复位异常会话
robotchat diagnose export           # 导出故障诊断包
```

## 部署

```bash
docker compose -f deploy/docker-compose.yml up -d          # 应用
docker compose -f deploy/docker-compose.models.yml up -d   # 模型服务
docker compose -f deploy/docker-compose.sim.yml up -d      # 仿真栈

deploy/scripts/install.sh        # 安装
deploy/scripts/selfcheck.sh      # 交付自检
deploy/scripts/upgrade.sh        # 升级
deploy/scripts/rollback.sh       # 回滚
deploy/scripts/backup.sh         # 备份 / restore.sh 恢复
```

部署拓扑、资源预算、安全加固与运维手册见
[deployment.md](./.trae/specs/full-duplex-voice-interaction/deployment.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [requirements.md](./.trae/specs/full-duplex-voice-interaction/requirements.md) | 需求、非功能指标、机型支持范围 |
| [design.md](./.trae/specs/full-duplex-voice-interaction/design.md) | 架构、模块、设备抽象与 ADR |
| [tasks.md](./.trae/specs/full-duplex-voice-interaction/tasks.md) | 任务分解与依赖 |
| [checklist.md](./.trae/specs/full-duplex-voice-interaction/checklist.md) | 验收清单 |
| [deployment.md](./.trae/specs/full-duplex-voice-interaction/deployment.md) | 部署、运维与现场验收 |

## 已知限制

- 5 款真机 Profile 为文档推导草稿，实测前不建议直接上机；需回填 `TODO-实测` 并跑 `audio profile-test`
- `assets/wakeword` 仅有英文唤醒词模型，中文模型 `wake_robot_v1.onnx` 待训练入库
- 熔断失败计数在 LLM / TTS 异常路径尚未回写，fallback 成功后主驱动熔断器会被重置（待修）
- 依赖白名单禁止 `torch` / `transformers` / `funasr` 等重型框架，本地 ASR 通过外部服务接入
