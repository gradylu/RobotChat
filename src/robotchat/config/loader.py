"""配置加载：默认配置 → 环境叠加 → 运行时变更 → 环境变量覆盖，然后强校验（§6.1）。"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from robotchat.config.schema import Settings, build_settings

ENV_PREFIX = "ROBOTCHAT__"
SECRET_KEY_HINTS = ("api_key", "token", "secret", "password", "credential")
_PLACEHOLDER = re.compile(r"^\${([A-Za-z_][A-Za-z0-9_]*)}$")

BASE_FILES = ("app.yaml", "providers.yaml", "tools.yaml", "intent.yaml")


class ConfigError(RuntimeError):
    """配置非法，启动必须失败（FAIL，不得带病启动）。"""

    def __init__(self, message: str, details: list[str] | None = None) -> None:
        self.details = details or []
        text = message if not self.details else message + "\n- " + "\n- ".join(self.details)
        super().__init__(text)


def resolve_paths(environ: Mapping[str, str] | None = None) -> tuple[Path, Path]:
    """返回 (配置目录, 数据目录)，均支持环境变量覆盖。"""
    env = os.environ if environ is None else environ
    config_dir = Path(env.get("ROBOTCHAT_CONFIG_DIR", "config"))
    data_dir = Path(env.get("ROBOTCHAT_DATA_DIR", "var"))
    return config_dir, data_dir


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fp:
            data = yaml.safe_load(fp) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 解析失败：{path}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件根节点必须是映射：{path}")
    return data


def deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """递归合并：映射深合并，其余类型以 overlay 为准。"""
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _set_path(target: dict[str, Any], keys: list[str], value: Any) -> None:
    node = target
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[keys[-1]] = value


def _parse_env_value(raw: str) -> Any:
    if raw == "":
        return ""
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def apply_env_overrides(
    raw: dict[str, Any], environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """应用 `ROBOTCHAT__<PATH>` 覆盖，`__` 分隔、小写映射到配置键。"""
    env = os.environ if environ is None else environ
    result = deepcopy(raw)
    for name in sorted(env):
        if not name.startswith(ENV_PREFIX):
            continue
        path = [part.lower() for part in name[len(ENV_PREFIX) :].split("__") if part]
        if not path:
            continue
        _set_path(result, path, _parse_env_value(env[name]))
    return result


def expand_placeholders(value: Any, environ: Mapping[str, str] | None = None) -> Any:
    """展开 `${VAR}` 形式的占位符；未定义的变量保持原样（便于启动时报错定位）。"""
    env = os.environ if environ is None else environ
    if isinstance(value, dict):
        return {key: expand_placeholders(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_placeholders(item, env) for item in value]
    if isinstance(value, str):
        match = _PLACEHOLDER.match(value)
        if match:
            return env.get(match.group(1), value)
        return value
    return value


def load_raw(
    config_dir: Path | None = None,
    data_dir: Path | None = None,
    env_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    include_runtime: bool = True,
) -> dict[str, Any]:
    """按优先级合并出原始配置 dict，不做 schema 校验。

    `include_runtime=False` 用于配置热更新：只读文件与环境变量，避免把已生效的
    运行时补丁重复叠加（也避免把展开后的密钥写回磁盘）。
    """
    default_config_dir, default_data_dir = resolve_paths(environ)
    config_dir = config_dir or default_config_dir
    data_dir = data_dir or default_data_dir
    env_map = os.environ if environ is None else environ

    if not config_dir.is_dir():
        raise ConfigError(f"配置目录不存在：{config_dir}")

    merged: dict[str, Any] = {}
    for filename in BASE_FILES:
        path = config_dir / filename
        if not path.exists():
            if filename == "app.yaml":
                raise ConfigError(f"缺少基础配置文件：{path}")
            continue
        merged = deep_merge(merged, _read_yaml(path))

    # 多环境叠加（默认层之上、运行时变更之下）
    overlay_name = env_name or env_map.get("ROBOTCHAT_ENV") or ""
    if overlay_name:
        overlay_path = config_dir / f"app.{overlay_name}.yaml"
        if overlay_path.exists():
            merged = deep_merge(merged, _read_yaml(overlay_path))
        merged.setdefault("app", {})["env"] = overlay_name

    # 运行时变更（WebUI / API 写入）
    runtime_path = data_dir / "config_active.yaml"
    if include_runtime and runtime_path.exists():
        merged = deep_merge(merged, _read_yaml(runtime_path))

    # 环境变量优先级最高
    merged = apply_env_overrides(merged, env_map)
    return expand_placeholders(merged, env_map)


def load_settings(
    config_dir: Path | None = None,
    data_dir: Path | None = None,
    env_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """加载并校验配置；非法配置抛 ConfigError（启动 FAIL）。"""
    raw = load_raw(config_dir=config_dir, data_dir=data_dir, env_name=env_name, environ=environ)
    try:
        return build_settings(raw)
    except ValidationError as exc:
        details = []
        for item in exc.errors():
            location = ".".join(str(part) for part in item["loc"])
            details.append(f"{location}: {item['msg']}")
        raise ConfigError("配置校验失败", details) from exc


def is_secret_key(key: str) -> bool:
    lowered = key.lower()
    if lowered.endswith("_env"):
        return False  # 环境变量名本身不是密钥
    return any(hint in lowered for hint in SECRET_KEY_HINTS)


def mask_secrets(value: Any) -> Any:
    """返回脱敏副本：密钥类字段一律替换为 `***`，空值保持空（§6.3）。"""
    if isinstance(value, dict):
        masked = {}
        for key, item in value.items():
            if is_secret_key(str(key)) and isinstance(item, str):
                masked[key] = "***" if item else ""
            else:
                masked[key] = mask_secrets(item)
        return masked
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    return value


def masked_settings(settings: Settings) -> dict[str, Any]:
    return mask_secrets(settings.model_dump(mode="json"))


# ------------------------------------------------ 热更新与回滚（T-6.3 / C-57、C-94）

ACTIVE_FILE = "config_active.yaml"
PREVIOUS_FILE = "config_previous.yaml"
HOT_SECTIONS: tuple[str, ...] = ("barge_in", "language", "providers", "tools", "intent")
RESTART_SECTIONS: tuple[str, ...] = (
    "app",
    "api",
    "audio",
    "vad",
    "state",
    "memory",
    "observability",
    "resilience",
)


class ConfigStore:
    """运行时配置补丁：校验 → 原子写入 → 生效；保留上一版本以支持回滚。

    补丁只记录「用户显式变更的字段」，不落盘合并后的完整配置，避免把环境变量
    展开出的密钥写进数据目录（FR-10.10）。
    """

    def __init__(
        self,
        config_dir: Path,
        data_dir: Path,
        *,
        env_name: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.config_dir = Path(config_dir)
        self.data_dir = Path(data_dir)
        self.env_name = env_name
        self._environ = environ

    @property
    def active_path(self) -> Path:
        return self.data_dir / ACTIVE_FILE

    @property
    def previous_path(self) -> Path:
        return self.data_dir / PREVIOUS_FILE

    def _env(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def patch(self) -> dict[str, Any]:
        """当前已生效的运行时补丁（无文件时为空 dict）。"""
        if not self.active_path.exists():
            return {}
        return _read_yaml(self.active_path)

    def previous(self) -> dict[str, Any] | None:
        if not self.previous_path.exists():
            return None
        return _read_yaml(self.previous_path)

    def base_raw(self) -> dict[str, Any]:
        """不含运行时补丁的原始配置（含环境变量覆盖与占位符展开）。"""
        return load_raw(
            config_dir=self.config_dir,
            data_dir=self.data_dir,
            env_name=self.env_name,
            environ=self._env(),
            include_runtime=False,
        )

    def candidate(self, patch: dict[str, Any]) -> Settings:
        """合并补丁并强校验；非法配置抛 ConfigError，不落盘。"""
        merged = deep_merge(self.base_raw(), patch)
        try:
            return build_settings(merged)
        except ValidationError as exc:
            details = [
                f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                for item in exc.errors()
            ]
            raise ConfigError("配置校验失败，已拒绝本次变更", details) from exc

    def apply(self, patch: dict[str, Any]) -> tuple[Settings, dict[str, Any]]:
        """合并补丁 → 校验 → 原子生效；返回 (新配置, 补丁快照)。"""
        merged_patch = deep_merge(self.patch(), patch)
        settings = self.candidate(merged_patch)  # 先校验，失败不触碰磁盘
        self._atomic_write(self.active_path, merged_patch, backup=True)
        return settings, merged_patch

    def rollback(self) -> tuple[Settings, dict[str, Any]]:
        """回滚到上一版本补丁；无历史版本时抛 ConfigError。"""
        previous = self.previous()
        if previous is None:
            raise ConfigError("没有可回滚的配置版本")
        settings = self.candidate(previous)
        self._atomic_write(self.active_path, previous, backup=False)
        return settings, previous

    def _atomic_write(
        self, path: Path, payload: dict[str, Any], *, backup: bool
    ) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if backup and path.exists():
            try:
                self.previous_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError as exc:  # 备份失败时拒绝变更，避免丢失回滚点
                raise ConfigError(f"配置备份失败：{exc}") from exc
        temp = path.with_suffix(".tmp")
        temp.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        os.replace(temp, path)


def changed_sections(before: Settings, after: Settings) -> dict[str, list[str]]:
    """对比两份配置，返回 {分段名: 变更字段路径}（用于热生效判定）。"""
    old = before.model_dump()
    new = after.model_dump()
    result: dict[str, list[str]] = {}
    for section in new:
        if old.get(section) == new[section]:
            continue
        result[section] = _diff_paths(old.get(section), new[section], section)
    return result


def _diff_paths(before: Any, after: Any, prefix: str) -> list[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        paths: list[str] = []
        for key in sorted(set(before) | set(after)):
            child_before = before.get(key)
            child_after = after.get(key)
            if child_before == child_after:
                continue
            paths.extend(_diff_paths(child_before, child_after, f"{prefix}.{key}"))
        return paths
    return [prefix]

