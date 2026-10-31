"""机型 Profile 加载 / 选路 / 诊断与 CLI 单测（T-1A.2、T-1A.6 / C-161、C-165、C-168）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from robotchat import cli as cli_module
from robotchat.audio.frame import AudioChunk, AudioFrame
from robotchat.audio.profile import (
    ProfileError,
    list_profiles,
    load_profile,
    resolve_profile,
    validate_distro,
)
from robotchat.cli import _build_parser, main
from robotchat.config.loader import load_settings
from robotchat.config.schema import AudioSettings, DeviceProfileSchema
from robotchat.observability.diagnostics import profile_report, profile_test

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


def _profile_raw(name: str, **overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "profile": name,
        "display_name": f"测试机型 {name}",
        "ros_distro": ["humble", "jazzy"],
        "robot_models": [f"model_{name}"],
        "adapter": "base_ros_audio",
        "capabilities": {
            "capture": True,
            "playback": True,
            "echo_reference": True,
            "hardware_aec": False,
            "volume_control": False,
            "playback_flush": "stream_stop",
        },
        "capture": {
            "topic": f"/{name}/mic",
            "msg_type": "audio_common_msgs/msg/AudioData",
            "sample_rate": 16000,
            "channels": 1,
            "bit_depth": 16,
        },
        "playback": {
            "topic": f"/{name}/speaker",
            "msg_type": "audio_common_msgs/msg/AudioData",
            "sample_rate": 16000,
            "channels": 1,
            "bit_depth": 16,
        },
    }
    raw.update(overrides)
    return raw


def _write_profile(config_dir: Path, name: str, raw: dict[str, Any] | None = None) -> Path:
    directory = config_dir / "devices"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    path.write_text(
        yaml.safe_dump(raw if raw is not None else _profile_raw(name), allow_unicode=True),
        encoding="utf-8",
    )
    return path


def _settings(environ: dict[str, str] | None = None):
    return load_settings(config_dir=REPO_CONFIG, data_dir=Path("var"), environ=environ or {})


def test_load_repo_profile() -> None:
    profile = load_profile(REPO_CONFIG, "generic_ros_audio")
    assert profile.profile == "generic_ros_audio"
    assert profile.adapter == "base_ros_audio"
    assert profile.capture.sample_rate == 16000
    assert profile.playback.sample_rate == 16000
    assert profile.capabilities.playback_flush == "service"
    assert profile.volume is not None


def test_list_profiles_sorted(tmp_path: Path) -> None:
    _write_profile(tmp_path, "zeta")
    _write_profile(tmp_path, "alpha")
    assert list(list_profiles(tmp_path)) == ["alpha", "zeta"]


def test_load_profile_name_must_match_file(tmp_path: Path) -> None:
    _write_profile(tmp_path, "renamed", _profile_raw("other"))
    with pytest.raises(ProfileError, match="名不一致"):
        load_profile(tmp_path, "renamed")


def test_unknown_profile_lists_available(tmp_path: Path) -> None:
    _write_profile(tmp_path, "known")
    with pytest.raises(ProfileError) as excinfo:
        load_profile(tmp_path, "missing")
    assert "未知机型 Profile" in str(excinfo.value)
    assert "known" in str(excinfo.value)


def test_invalid_profile_field_rejected(tmp_path: Path) -> None:
    raw = _profile_raw("broken")
    raw["capture"].pop("sample_rate")
    _write_profile(tmp_path, "broken", raw)
    with pytest.raises(ProfileError, match="字段非法"):
        load_profile(tmp_path, "broken")


def test_non_mapping_or_bad_yaml_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "devices"
    directory.mkdir(parents=True)
    (directory / "list.yaml").write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="必须是对象"):
        load_profile(tmp_path, "list")

    (directory / "bad.yaml").write_text("profile: [unclosed\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="解析失败"):
        load_profile(tmp_path, "bad")


def test_distro_mismatch_rejected(tmp_path: Path) -> None:
    _write_profile(tmp_path, "humble_only", _profile_raw("humble_only", ros_distro=["humble"]))
    profile = load_profile(tmp_path, "humble_only")
    with pytest.raises(ProfileError, match="不匹配"):
        validate_distro(profile, "jazzy")
    validate_distro(profile, "humble")
    validate_distro(profile, None)  # 现场未声明发行版时跳过


def test_resolve_explicit_prefers_config_and_warns(tmp_path: Path) -> None:
    _write_profile(tmp_path, "explicit")
    audio = AudioSettings(device_profile="explicit", auto_detect_profile=True)
    resolution = resolve_profile(
        audio, tmp_path, environ={"ROBOTCHAT__ROBOT_MODEL": "model_explicit"}
    )
    assert resolution.profile.profile == "explicit"
    assert resolution.source == "explicit"
    assert "忽略自动探测" in resolution.warnings[0]


def test_resolve_auto_matches_robot_model(tmp_path: Path) -> None:
    _write_profile(tmp_path, "auto_hit")
    audio = AudioSettings(auto_detect_profile=True)
    resolution = resolve_profile(
        audio, tmp_path, environ={"ROBOTCHAT__ROBOT_MODEL": "model_auto_hit"}
    )
    assert resolution.profile.profile == "auto_hit"
    assert resolution.source == "auto"
    assert resolution.warnings == ()


def test_resolve_auto_ambiguous_falls_back_with_warning(tmp_path: Path) -> None:
    _write_profile(tmp_path, "first", _profile_raw("first", robot_models=["shared"]))
    _write_profile(tmp_path, "second", _profile_raw("second", robot_models=["shared"]))
    _write_profile(tmp_path, "fallback", _profile_raw("fallback", robot_models=["other"]))
    audio = AudioSettings(auto_detect_profile=True, fallback_profile="fallback")
    resolution = resolve_profile(audio, tmp_path, environ={"ROBOTCHAT__ROBOT_MODEL": "shared"})
    assert resolution.profile.profile == "fallback"
    assert resolution.source == "fallback"
    assert "匹配到 2 个 Profile" in resolution.warnings[0]


def test_resolve_auto_failure_without_fallback_rejects_startup(tmp_path: Path) -> None:
    _write_profile(tmp_path, "only")
    audio = AudioSettings(auto_detect_profile=True)
    with pytest.raises(ProfileError, match="拒绝进入就绪态"):
        resolve_profile(audio, tmp_path, environ={"ROBOTCHAT__ROBOT_MODEL": "unknown_model"})


def test_resolve_fallback_without_auto(tmp_path: Path) -> None:
    _write_profile(tmp_path, "safe")
    audio = AudioSettings(fallback_profile="safe")
    resolution = resolve_profile(audio, tmp_path, environ={})
    assert resolution.profile.profile == "safe"
    assert resolution.source == "fallback"
    assert "audio.fallback_profile" in resolution.warnings[0]


def test_resolve_distro_mismatch_rejects(tmp_path: Path) -> None:
    _write_profile(tmp_path, "humble_only", _profile_raw("humble_only", ros_distro=["humble"]))
    audio = AudioSettings(device_profile="humble_only")
    with pytest.raises(ProfileError):
        resolve_profile(audio, tmp_path, environ={"ROS_DISTRO": "jazzy"})


def test_profile_report_ok_and_error(tmp_path: Path) -> None:
    settings = _settings()
    report = profile_report(settings, REPO_CONFIG, environ={"ROS_DISTRO": "humble"})
    assert report["ok"] is True
    assert report["resolved"]["profile"] == "generic_ros_audio"
    assert report["resolved"]["source"] == "explicit"
    assert "generic_ros_audio" in report["available"]
    assert "generic_ros_audio" in report["detail"]

    broken = settings.model_copy(update={"audio": AudioSettings(device_profile="missing")})
    failed = profile_report(broken, REPO_CONFIG, environ={})
    assert failed["ok"] is False
    assert failed["resolved"] is None
    assert "未知机型 Profile" in failed["detail"]


class _FakeCapture:
    def __init__(self, frames: list[AudioFrame]) -> None:
        self._frames = frames

    async def open(self) -> None:
        return None

    def stream(self):
        async def generator():
            for frame in self._frames:
                yield frame

        return generator()

    def last_timestamp(self) -> float:
        return self._frames[-1].ts_device if self._frames else 0.0

    async def close(self) -> None:
        return None


class _FakePlayback:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.played = 0
        self.flushes = 0

    async def open(self) -> None:
        return None

    async def play(self, chunk: AudioChunk) -> None:
        if self.fail:
            raise RuntimeError("模拟播放失败")
        self.played += 1

    async def flush(self) -> None:
        self.flushes += 1

    async def set_volume(self, level: float) -> None:
        return None

    async def close(self) -> None:
        return None


class _FakeAdapter:
    def __init__(self, frames: int = 5, playback_fail: bool = False) -> None:
        self.profile_name = "fake_profile"
        self.reconnects = 0
        self._frames = [
            AudioFrame(data=bytes(640), sample_rate=16000, ts_device=index * 0.02, seq=index + 1)
            for index in range(frames)
        ]
        self.capture = _FakeCapture(self._frames)
        self.playback = _FakePlayback(fail=playback_fail)
        self.closed = False

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


async def test_profile_test_passes_with_injected_adapter() -> None:
    profile: DeviceProfileSchema = load_profile(REPO_CONFIG, "generic_ros_audio")
    adapter = _FakeAdapter(frames=5)
    result = await profile_test(profile, adapter=adapter, duration_s=0.01)

    assert result.ok is True
    assert result.capture_ok is True
    assert result.playback_ok is True
    assert result.flush_ok is True
    assert result.frames == 5
    assert result.timestamp_monotonic is True
    assert adapter.playback.played == 1
    assert adapter.closed is True
    assert "generic_ros_audio" in result.detail


async def test_profile_test_flags_silent_capture_and_playback_failure() -> None:
    profile: DeviceProfileSchema = load_profile(REPO_CONFIG, "generic_ros_audio")

    silent = await profile_test(profile, adapter=_FakeAdapter(frames=0), duration_s=0.01)
    assert silent.ok is False
    assert silent.capture_ok is False
    assert "未收到采集数据" in silent.detail

    failing = await profile_test(
        profile, adapter=_FakeAdapter(frames=5, playback_fail=True), duration_s=0.01
    )
    assert failing.ok is False
    assert failing.playback_ok is False
    assert "播放失败" in failing.detail


def test_parser_supports_profile_commands() -> None:
    parser = _build_parser()
    devices = parser.parse_args(["audio", "devices"])
    assert devices.group == "audio"
    assert devices.command == "devices"

    tested = parser.parse_args(["audio", "profile-test", "--profile", "generic_ros_audio"])
    assert tested.command == "profile-test"
    assert tested.profile == "generic_ros_audio"
    assert tested.duration == 1.0


def test_cli_audio_devices_prints_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ROBOTCHAT_CONFIG_DIR", str(REPO_CONFIG))
    monkeypatch.setenv("ROBOTCHAT_DATA_DIR", str(tmp_path))
    # 真实 configure_logging 会把 capsys 的 stdout 固化进 PrintLogger，
    # 测试结束后该流被关闭会污染后续用例日志，故此处替换为无操作。
    monkeypatch.setattr(cli_module, "configure_logging", lambda **_: None)

    assert main(["audio", "devices"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["resolved"]["profile"] == "generic_ros_audio"
    assert "generic_ros_audio" in payload["available"]
