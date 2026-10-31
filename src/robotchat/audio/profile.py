"""机型 Profile 加载、校验与选路（T-1A.2 / FR-16.3、FR-16.9、FR-16.11）。

- `config/devices/<name>.yaml` 描述某机型的采集 / 播放接口细节；
- 显式 `audio.device_profile` 优先；未显式时按 `robot_models` 自动匹配；
- 自动匹配失败：有 `audio.fallback_profile` 则使用并 WARN，否则**拒绝启动**；
- `ros_distro` 与当前镜像 `ROS_DISTRO` 不一致时直接报错。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from robotchat.config.schema import AudioSettings, DeviceProfileSchema

PROFILE_DIRNAME = "devices"


class ProfileError(ValueError):
    """Profile 缺失 / 非法 / 与现场不匹配：必须拒绝就绪（FR-16.9）。"""


@dataclass(frozen=True, slots=True)
class ProfileResolution:
    profile: DeviceProfileSchema
    source: str  # explicit | auto | fallback
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile.profile,
            "display_name": self.profile.display_name,
            "source": self.source,
            "ros_distro": list(self.profile.ros_distro),
            "warnings": list(self.warnings),
        }


def profile_dir(config_dir: Path) -> Path:
    return Path(config_dir) / PROFILE_DIRNAME


def profile_path(config_dir: Path, name: str) -> Path:
    return profile_dir(config_dir) / f"{name}.yaml"


def list_profiles(config_dir: Path) -> dict[str, Path]:
    """列出已注册 Profile（文件名 → 路径），按名称排序。"""
    directory = profile_dir(config_dir)
    if not directory.is_dir():
        return {}
    return {
        item.stem: item
        for item in sorted(directory.glob("*.yaml"), key=lambda path: path.stem)
    }


def load_profile(config_dir: Path, name: str) -> DeviceProfileSchema:
    """按名称加载并校验 Profile；任何缺失 / 字段错误都给出可操作提示。"""
    path = profile_path(config_dir, name)
    if not path.is_file():
        available = ", ".join(list_profiles(config_dir)) or "(无)"
        raise ProfileError(
            f"未知机型 Profile：{name!r}（未找到 {path}；可用：{available}）"
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"机型 Profile 解析失败：{path}：{exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"机型 Profile 内容必须是对象：{path}")
    try:
        profile = DeviceProfileSchema.model_validate(raw)
    except ValidationError as exc:
        raise ProfileError(f"机型 Profile 字段非法：{path}：{exc}") from exc
    if profile.profile != name:
        raise ProfileError(
            f"机型 Profile 名不一致：文件名 {name!r} 与内容 profile={profile.profile!r}"
        )
    return profile


def validate_distro(profile: DeviceProfileSchema, ros_distro: str | None) -> None:
    """镜像发行版与 Profile 支持列表必须匹配（design §3.6.3 第 2 步）。"""
    if not ros_distro or not profile.ros_distro:
        return
    if ros_distro not in profile.ros_distro:
        raise ProfileError(
            f"机型 Profile {profile.profile!r} 声明 ros_distro={profile.ros_distro}，"
            f"当前镜像为 {ros_distro!r}，不匹配（请换用兼容镜像或修正 Profile）"
        )


def resolve_profile(
    audio: AudioSettings,
    config_dir: Path,
    environ: Mapping[str, str] | None = None,
) -> ProfileResolution:
    """按配置选出最终 Profile（FR-16.3 / FR-16.11）。"""
    env = os.environ if environ is None else environ
    distro = env.get("ROS_DISTRO") or None
    explicit = (audio.device_profile or "").strip()

    def finalize(name: str, source: str, warnings: tuple[str, ...] = ()) -> ProfileResolution:
        profile = load_profile(config_dir, name)
        validate_distro(profile, distro)
        return ProfileResolution(profile=profile, source=source, warnings=warnings)

    if explicit:
        notes: tuple[str, ...] = ()
        if audio.auto_detect_profile:
            notes = ("已显式指定 audio.device_profile，忽略自动探测",)
        return finalize(explicit, "explicit", notes)

    if audio.auto_detect_profile:
        robot_model = (env.get("ROBOTCHAT__ROBOT_MODEL") or env.get("ROBOT_MODEL") or "").strip()
        matches: list[str] = []
        for name in list_profiles(config_dir):
            try:
                profile = load_profile(config_dir, name)
            except ProfileError:
                continue
            if robot_model and robot_model in profile.robot_models:
                matches.append(name)
        if len(matches) == 1:
            return finalize(matches[0], "auto")
        reason = (
            "未设置 ROBOTCHAT__ROBOT_MODEL，无法自动匹配"
            if not robot_model
            else f"robot_model={robot_model!r} 匹配到 {len(matches)} 个 Profile"
        )
        if audio.fallback_profile:
            return finalize(
                audio.fallback_profile,
                "fallback",
                (f"{reason}；已按 audio.fallback_profile={audio.fallback_profile!r} 兜底",),
            )
        raise ProfileError(
            f"机型 Profile 自动匹配失败：{reason}；未配置 audio.fallback_profile，"
            "拒绝进入就绪态（FR-16.11）。请手工指定 audio.device_profile 或运行 "
            "`robotchat audio devices` 查看可用 Profile"
        )

    if audio.fallback_profile:
        return finalize(
            audio.fallback_profile,
            "fallback",
            ("未指定 audio.device_profile；已按 audio.fallback_profile 兜底",),
        )
    raise ProfileError(
        "未指定 audio.device_profile 且 auto_detect_profile=false，"
        "也没有 audio.fallback_profile；拒绝进入就绪态（FR-16.9）"
    )


__all__ = [
    "PROFILE_DIRNAME",
    "ProfileError",
    "ProfileResolution",
    "list_profiles",
    "load_profile",
    "profile_dir",
    "profile_path",
    "resolve_profile",
    "validate_distro",
]
