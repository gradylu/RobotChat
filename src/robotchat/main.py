"""FastAPI 入口与生命周期管理（T-0.10）。

配置在启动时强校验：非法配置直接 FAIL 阻塞启动（FR-10.9）。
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from robotchat import __version__
from robotchat.api import rest, ws
from robotchat.config.loader import ConfigError, load_settings, resolve_paths
from robotchat.config.schema import Settings
from robotchat.observability.logging import (
    configure_logging,
    get_logger,
    log_dir_from_env,
    log_retention_days_from_env,
)
from robotchat.runtime import build_runtime

# WebUI 构建产物目录（T-6.5）：不存在时只提供 API，不影响后端独立运行
WEBUI_DIST = Path(__file__).resolve().parents[2] / "webui" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    preset: Settings | None = getattr(app.state, "preset_settings", None)
    try:
        settings = preset or load_settings()
    except ConfigError as exc:
        # 启动失败必须显式暴露具体字段，不允许带病启动
        configure_logging()
        get_logger("robotchat.main").error("config_invalid", error=str(exc))
        raise

    configure_logging(
        level=settings.observability.log_level,
        json_output=settings.observability.log_json,
        log_dir=log_dir_from_env(),
        retention_days=log_retention_days_from_env(),
    )
    config_dir, data_dir = resolve_paths()
    try:
        runtime = build_runtime(settings, config_dir, data_dir)
    except ConfigError as exc:
        # 鉴权口令过短等运行期配置错误同样拒绝启动（C-98）
        get_logger("robotchat.main").error("runtime_config_invalid", error=str(exc))
        raise
    app.state.runtime = runtime
    await runtime.startup()

    logger = get_logger("robotchat.main")
    logger.info(
        "service_started",
        version=__version__,
        env=settings.app.env,
        protocol_version=settings.api.protocol_version,
        transport=settings.audio.transport,
    )
    try:
        yield
    finally:
        await runtime.shutdown()
        logger.info("service_stopped", uptime_s=runtime.uptime_s())


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="RobotChat",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.preset_settings = settings
    app.include_router(rest.router)
    app.include_router(ws.router)
    # 静态资源最后挂载，避免遮蔽 /api 与 /ws（T-6.5 / C-111）
    if WEBUI_DIST.is_dir():
        app.mount("/", StaticFiles(directory=str(WEBUI_DIST), html=True), name="webui")
    return app


app = create_app()


def main() -> None:
    import uvicorn

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"配置校验失败，拒绝启动：\n{exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    uvicorn.run(
        create_app(settings),
        host=settings.api.host,
        port=settings.api.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
