"""结构化日志：JSON 输出，自动携带 session_id / turn_id / state 上下文。

stdout 走 Docker json-file 轮转；设置 `ROBOTCHAT_LOG_DIR` 时额外按天写 JSONL
（`robotchat-YYYY-MM-DD.jsonl`），并清理超过保留天数的旧文件（T-7.10）。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars
from structlog.typing import Processor

_configured = False
DEFAULT_RETENTION_DAYS = 14


class DailyJsonlWriter:
    """按天切分 JSONL；跨天后自动清理超出保留期的历史文件。"""

    def __init__(self, log_dir: str | Path, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        self.log_dir = Path(log_dir)
        self.retention_days = max(1, retention_days)
        self._day = ""
        self._path: Path | None = None

    def __call__(
        self, _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            day = time.strftime("%Y-%m-%d")
            if day != self._day:
                self._day = day
                self._path = self.log_dir / f"robotchat-{day}.jsonl"
                self._prune()
            if self._path is not None:
                line = json.dumps(event_dict, ensure_ascii=False, default=str)
                with self._path.open("a", encoding="utf-8") as fp:
                    fp.write(line + "\n")
        except OSError:  # 日志落盘失败不应影响业务
            pass
        return event_dict

    def _prune(self) -> None:
        cutoff = time.time() - self.retention_days * 86400
        for path in self.log_dir.glob("robotchat-*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue


def configure_logging(
    level: str = "INFO",
    json_output: bool = True,
    log_dir: str | Path | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> None:
    """配置全局日志。重复调用不报错，便于测试与热更配置。"""
    global _configured

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer()
    )

    processors = list(shared)
    if log_dir:
        processors.append(DailyJsonlWriter(log_dir, retention_days))
    processors.append(renderer)

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level.upper()) if level else logging.NOTSET
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    _configured = True


def log_dir_from_env(environ: Any = None) -> Path | None:
    """读取显式配置的日志目录；未设置则只输出 stdout（测试环境不落盘）。"""
    env = os.environ if environ is None else environ
    value = env.get("ROBOTCHAT_LOG_DIR", "").strip()
    return Path(value) if value else None


def log_retention_days_from_env(environ: Any = None) -> int:
    """日志保留天数（`ROBOTCHAT_LOG_RETENTION_DAYS`，默认 14，T-7.10）。"""
    env = os.environ if environ is None else environ
    raw = str(env.get("ROBOTCHAT_LOG_RETENTION_DAYS", "") or "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_RETENTION_DAYS
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)


def bind_context(**kwargs: Any) -> None:
    """绑定请求级上下文（如 session_id / turn_id / state），随后的日志自动携带。"""
    bind_contextvars(**kwargs)


def clear_context() -> None:
    clear_contextvars()
