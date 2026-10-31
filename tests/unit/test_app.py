"""应用入口与健康端点单测（T-0.10）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from robotchat.config.loader import ConfigError, load_settings
from robotchat.main import create_app

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    # 单测不触网：关闭公网离线探测与云端 fallback，只验证装配与降级可见性
    settings = load_settings(
        config_dir=REPO_CONFIG,
        data_dir=tmp_path,
        environ={
            "ROBOTCHAT__RESILIENCE__OFFLINE_PROBE_ON_START": "false",
            "ROBOTCHAT__PROVIDERS__ASR__FALLBACK": "",
            "ROBOTCHAT__PROVIDERS__LLM__FALLBACK": "",
            "ROBOTCHAT__PROVIDERS__TTS__FALLBACK": "",
        },
    )
    # 用上下文管理器进入 lifespan，runtime 才会挂到 app.state
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_live_and_health(client: TestClient) -> None:
    assert client.get("/api/live").json() == {"status": "alive"}

    health = client.get("/api/health")
    assert health.status_code == 200
    body = health.json()
    assert body["ready"] is True
    assert body["app"]["name"] == "robotchat"
    assert body["subsystems"]["audio"]["device_profile"] == "generic_ros_audio"
    # T-1A：生效 Profile 及来源须在健康端点可见
    resolved = body["subsystems"]["audio"]["resolved_profile"]
    assert resolved["name"] == "generic_ros_audio"
    assert resolved["source"] == "explicit"
    assert resolved["warnings"] == []
    # C-171：auto 的实际选择结果与原因须可见（默认无硬件 AEC → 软件 NLMS）
    aec = body["subsystems"]["audio"]["aec"]
    assert aec["requested"] == "auto"
    assert aec["effective"] == "software"
    assert aec["fallback_notice"] is True
    assert aec["detail"]
    # 本地模型服务未启动：驱动可实例化但健康探测失败 → degraded（不再标记 unavailable）
    assert body["subsystems"]["drivers"]["asr"]["state"] == "degraded"
    assert body["subsystems"]["drivers"]["asr"]["active"] == "funasr_local"
    assert body["subsystems"]["drivers"]["tts"]["state"] == "degraded"
    # LLM 地址未配置（占位符未展开）→ 初始化失败，该类别 unavailable
    assert body["subsystems"]["drivers"]["llm"]["state"] == "unavailable"
    # T-2.10 / T-2.12：离线状态与 RAG 计数可在健康端点读取
    assert body["subsystems"]["offline"]["state"] == "online"
    assert body["subsystems"]["rag"]["rag_skipped"] == 0
    assert "asr" in body["subsystems"]["providers"]


def test_providers_endpoint_exposes_capabilities(client: TestClient) -> None:
    response = client.get("/api/providers")
    assert response.status_code == 200
    body = response.json()
    providers = body["providers"]
    assert {"asr", "llm", "tts"} <= set(providers)
    assert providers["asr"]["circuit"]["state"] == "CLOSED"
    assert providers["asr"]["capabilities"]["streaming"] is True
    assert "features" in providers["llm"]
    assert body["offline"]["state"] == "online"


def test_tools_endpoint_lists_risk_levels(client: TestClient) -> None:
    """C-80：`/api/tools` 暴露工具定义与风险等级。"""
    body = client.get("/api/tools").json()
    assert body["enabled"] is True
    assert set(body["enabled_tools"]) == {"navigate_to", "set_io", "speak"}
    risks = {item["name"]: item["risk"] for item in body["definitions"]}
    assert risks == {"speak": "low", "navigate_to": "high", "set_io": "medium"}


def test_tool_invoke_endpoint_runs_builtin(client: TestClient) -> None:
    response = client.post("/api/tools/speak/invoke", json={"args": {"text": "你好"}})
    assert response.status_code == 200
    body = response.json()
    assert body["invoked"] is True
    assert body["ok"] is True
    assert body["output"] == "你好"


def test_ready(client: TestClient) -> None:
    response = client.get("/api/ready")
    assert response.status_code == 200
    assert response.json()["ready"] is True


def test_config_is_masked(client: TestClient) -> None:
    config = client.get("/api/config").json()
    assert config["api"]["protocol_version"] == "v1"
    # 模板中的密钥项为空，且不允许出现明文
    assert "sk-" not in str(config)


def test_metrics_endpoint(client: TestClient) -> None:
    response = client.get("/api/metrics")
    assert response.status_code == 200
    body = response.json()
    assert "stages" in body
    # 语言维度指标（C-158）随 /api/metrics 暴露（T-2A.7）
    assert "languages" in body
    assert "asr_success_rate_by_lang" in body["languages"]


def test_languages_endpoint(client: TestClient) -> None:
    response = client.get("/api/languages")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "auto"
    assert "zh" in body["supported"]
    assert body["stats"]["language_switch_count"] == 0


def test_language_switch_and_event(client: TestClient) -> None:
    response = client.post("/api/languages", json={"mode": "fixed", "lang": "en"})
    assert response.status_code == 200
    body = response.json()
    assert body["updated"] is True
    assert body["mode"] == "fixed"
    assert body["current"] == "en"

    events = client.get("/api/logs/events").json()["events"]
    assert any(event["type"] == "language.changed" for event in events)

    # 恢复自动模式，避免影响共享默认会话
    client.post("/api/languages", json={"mode": "auto"})


def test_language_switch_rejects_unknown(client: TestClient) -> None:
    response = client.post("/api/languages", json={"lang": "xx"})
    assert response.status_code == 400
    assert response.json()["updated"] is False


def test_logs_events_export(client: TestClient) -> None:
    response = client.get("/api/logs/events")
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "default"
    assert body["count"] == len(body["events"])

    replay = client.get("/api/logs/events", params={"replay": "true"}).json()
    assert {"session", "turns", "events"} <= set(replay)


def test_session_reset(client: TestClient) -> None:
    response = client.post("/api/session/default/reset")
    assert response.status_code == 200
    body = response.json()
    assert body["reset"] is True
    assert body["state"] in {"IDLE", "ACTIVE_IDLE"}


def test_invalid_config_blocks_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBOTCHAT_CONFIG_DIR", str(tmp_path))
    (tmp_path / "app.yaml").write_text("audio: {device_profile: ''}\n", encoding="utf-8")

    with pytest.raises(ConfigError), TestClient(create_app()):
        pass
