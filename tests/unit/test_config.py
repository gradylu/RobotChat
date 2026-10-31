"""配置加载、叠加、校验与脱敏单测（T-0.3）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from robotchat.config.loader import (
    ConfigError,
    apply_env_overrides,
    deep_merge,
    load_raw,
    load_settings,
    masked_settings,
)
from robotchat.config.schema import build_settings

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


def _minimal_raw() -> dict[str, Any]:
    return {
        "app": {"name": "robotchat", "env": "dev"},
        "audio": {
            "transport": "file",
            "device_profile": "generic_ros_audio",
            "capture": {"target_sample_rate": 16000},
            "playback": {"queue_ms": 300, "flush_budget_ms": 100},
            "aec": {"mode": "auto"},
        },
        "providers": {
            "asr": {
                "active": "local",
                "drivers": {
                    "local": {"cloud": False},
                    "cloud": {"cloud": True},
                },
            },
            "llm": {"active": "mock", "drivers": {"mock": {"base_url": ""}}},
            "tts": {"active": "mock", "drivers": {"mock": {"voice": "default"}}},
            "knowledge": {"active": "none", "drivers": {}},
            "wakeword": {
                "enabled": True,
                "active": "mock",
                "drivers": {"mock": {"threshold": 0.6}},
            },
        },
        "tools": {"policy": {"confirm_timeout_ms": 10000}, "definitions": {}},
    }


def test_load_repo_config() -> None:
    settings = load_settings(config_dir=REPO_CONFIG, data_dir=Path("var"), environ={})
    assert settings.providers.asr.active == "funasr_local"
    assert settings.audio.device_profile == "generic_ros_audio"
    assert settings.tools.policy.confirm_timeout_ms == 10000
    assert settings.vad.silence_ms == 700


def test_deep_merge_overrides_only_scalars() -> None:
    base = {"audio": {"aec": {"mode": "auto", "fallback_notice": True}}, "v": 1}
    merged = deep_merge(base, {"audio": {"aec": {"mode": "software"}}})
    assert merged["audio"]["aec"] == {"mode": "software", "fallback_notice": True}
    assert merged["v"] == 1


def test_env_override_nested_value() -> None:
    raw = apply_env_overrides(
        _minimal_raw(),
        {
            "ROBOTCHAT__AUDIO__AEC__MODE": "software",
            "ROBOTCHAT__PROVIDERS__LLM__ACTIVE": "mock",
            "ROBOTCHAT__TOOLS__POLICY__CONFIRM_TIMEOUT_MS": "9000",
        },
    )
    assert raw["audio"]["aec"]["mode"] == "software"
    assert raw["tools"]["policy"]["confirm_timeout_ms"] == 9000


def test_env_override_beats_repo_config(tmp_path: Path) -> None:
    settings = load_settings(
        config_dir=REPO_CONFIG,
        data_dir=tmp_path,
        environ={"ROBOTCHAT__AUDIO__AEC__MODE": "software"},
    )
    assert settings.audio.aec.mode == "software"


def test_env_overlay_file(tmp_path: Path) -> None:
    (tmp_path / "app.yaml").write_text(
        yaml.safe_dump(_minimal_raw(), allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "providers.yaml").write_text(
        yaml.safe_dump({"providers": _minimal_raw()["providers"]}, allow_unicode=True),
        encoding="utf-8",
    )
    (tmp_path / "tools.yaml").write_text(
        yaml.safe_dump({"tools": _minimal_raw()["tools"]}), encoding="utf-8"
    )
    (tmp_path / "app.dev.yaml").write_text(
        yaml.safe_dump({"audio": {"aec": {"mode": "none"}}}), encoding="utf-8"
    )
    settings = load_settings(config_dir=tmp_path, data_dir=tmp_path, env_name="dev", environ={})
    assert settings.audio.aec.mode == "none"
    assert settings.app.env == "dev"


def test_placeholder_expansion(tmp_path: Path) -> None:
    raw = load_raw(
        config_dir=REPO_CONFIG,
        data_dir=tmp_path,
        environ={"LLM_BASE_URL": "http://10.0.0.12:8000/v1", "LLM_MODEL": "qwen"},
    )
    drivers = raw["providers"]["llm"]["drivers"]["edge_llm"]
    assert drivers["base_url"] == "http://10.0.0.12:8000/v1"
    assert drivers["model"] == "qwen"


def test_invalid_active_driver_rejected() -> None:
    raw = _minimal_raw()
    raw["providers"]["asr"]["active"] = "not_exist"
    with pytest.raises(Exception, match="未在 drivers 中定义"):
        build_settings(raw)


def test_open_ear_cloud_asr_requires_ack() -> None:
    raw = _minimal_raw()
    raw["providers"]["wakeword"]["enabled"] = False
    raw["providers"]["asr"]["active"] = "cloud"
    with pytest.raises(Exception, match="cloud_asr_ack"):
        build_settings(raw)

    raw["providers"]["wakeword"]["open_ear"] = {"cloud_asr_ack": True}
    settings = build_settings(raw)
    assert settings.providers.wakeword.open_ear.cloud_asr_ack is True


def test_open_ear_local_asr_needs_no_ack() -> None:
    raw = _minimal_raw()
    raw["providers"]["wakeword"]["enabled"] = False
    settings = build_settings(raw)
    assert settings.providers.asr.active == "local"


def test_resident_state_must_not_have_timeout() -> None:
    raw = _minimal_raw()
    raw["state"] = {"timeouts_ms": {"IDLE": 1000}}
    with pytest.raises(Exception, match="常驻等待状态"):
        build_settings(raw)


def test_missing_device_profile_rejected() -> None:
    raw = _minimal_raw()
    raw["audio"]["device_profile"] = ""
    with pytest.raises(Exception, match="device_profile"):
        build_settings(raw)


def test_language_routing_references_unknown_driver() -> None:
    raw = _minimal_raw()
    raw["language"] = {"asr_routing": {"yue": "not_registered"}}
    with pytest.raises(Exception, match="asr_routing"):
        build_settings(raw)


def test_language_fallback_must_be_supported() -> None:
    raw = _minimal_raw()
    raw["language"] = {"supported": ["zh", "en"], "fallback": "fr"}
    with pytest.raises(Exception, match="fallback"):
        build_settings(raw)


def test_language_tts_voice_references_unknown_driver() -> None:
    raw = _minimal_raw()
    raw["language"] = {"tts_voices": {"yue": {"driver": "missing_tts"}}}
    with pytest.raises(Exception, match="tts_voices"):
        build_settings(raw)


def test_secret_masking() -> None:
    raw = _minimal_raw()
    raw["providers"]["llm"]["drivers"]["mock"]["api_key"] = "sk-secret"
    raw["providers"]["asr"]["drivers"]["local"]["api_key_env"] = "DASHSCOPE_API_KEY"
    settings = build_settings(raw)
    masked = masked_settings(settings)
    assert masked["providers"]["llm"]["drivers"]["mock"]["api_key"] == "***"
    assert masked["providers"]["asr"]["drivers"]["local"]["api_key_env"] == "DASHSCOPE_API_KEY"


def test_missing_config_dir() -> None:
    with pytest.raises(ConfigError, match="配置目录不存在"):
        load_settings(config_dir=Path("/not/exist"), environ={})
