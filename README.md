# RobotChat

机器人本体全双工语音交互系统。需求与设计见 [.trae/specs/full-duplex-voice-interaction](./.trae/specs/full-duplex-voice-interaction/)。

## 本地开发

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows
pip install -e ".[dev]"
python -m robotchat.main       # 启动服务，默认 http://127.0.0.1:8080
pytest
```

## 配置

- 默认模板：`config/app.yaml`、`config/providers.yaml`、`config/tools.yaml`
- 多环境叠加：`ROBOTCHAT_ENV=dev|sim|robot` → 追加 `config/app.<env>.yaml` 深合并
- 环境变量覆盖：`ROBOTCHAT__` + 配置路径（`__` 分隔、大写），如 `ROBOTCHAT__AUDIO__AEC__MODE=software`
- 优先级：环境变量 > 运行时变更 > 现场配置 > 默认配置
