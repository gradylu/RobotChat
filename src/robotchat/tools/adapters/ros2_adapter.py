"""ROS2 工具执行适配器（T-5.6、C-81）。

- 目标与调用方式（service / action）由 `config/tools.yaml` 定义决定；
- rclpy 采用惰性导入，未装 ROS2 的环境只报明确错误，不影响服务启动；
- 后端可注入，便于用桩后端跑单测（真实联调在 T-8.5 完成）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

from robotchat.core.cancel import CancellationToken
from robotchat.health import HealthStatus
from robotchat.observability.logging import get_logger
from robotchat.tools.base import ToolCall, ToolResult
from robotchat.tools.registry import register_adapter

logger = get_logger("robotchat.tools.adapters.ros2")


class Ros2Backend(Protocol):
    """最小后端契约：只覆盖本期需要的 service / action 两类调用。"""

    async def call_service(self, target: str, args: dict[str, Any]) -> Any: ...

    async def send_action(self, target: str, args: dict[str, Any]) -> Any: ...

    async def aclose(self) -> None: ...


class RclpyBackend:
    """容器内直连 DDS 的默认后端（方案 A）；阻塞调用放入线程池执行。"""

    def __init__(self) -> None:
        self._module: Any | None = None
        self._node: Any | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> Any:
        if self._module is not None:
            return self._module
        async with self._lock:
            if self._module is None:
                try:
                    import rclpy
                except ImportError as exc:  # pragma: no cover - 依赖现场环境
                    raise RuntimeError("rclpy 不可用：请使用 ROS2 基础镜像方案 A") from exc
                if not rclpy.ok():
                    rclpy.init(args=None)
                self._module = rclpy
                self._node = rclpy.create_node("robotchat_tool_adapter")
        return self._module

    async def call_service(self, target: str, args: dict[str, Any]) -> Any:
        return await asyncio.to_thread(self._call_service_sync, target, args)

    def _call_service_sync(self, target: str, args: dict[str, Any]) -> Any:
        rclpy = self._module
        node = self._node
        if rclpy is None or node is None:
            raise RuntimeError("ROS2 后端未初始化")
        from std_srvs.srv import Trigger  # type: ignore[import-not-found]

        client = node.create_client(Trigger, target)
        if not client.wait_for_service(timeout_sec=2.0):
            raise RuntimeError(f"ROS2 服务不可达：{target}")
        request = Trigger.Request()
        for key, value in args.items():
            setattr(request, str(key), value)
        future = client.call_async(request)
        deadline = time.monotonic() + 30.0
        while not future.done() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not future.done():
            raise RuntimeError(f"ROS2 服务调用超时：{target}")
        return future.result()

    async def send_action(self, target: str, args: dict[str, Any]) -> Any:
        return await asyncio.to_thread(self._send_action_sync, target, args)

    def _send_action_sync(self, target: str, args: dict[str, Any]) -> Any:
        raise RuntimeError(
            f"ROS2 action 调用需按机型定义具体 Action 类型：{target}（args={args}）"
        )

    async def aclose(self) -> None:
        if self._node is not None:
            self._node.destroy_node()
            self._node = None
        if self._module is not None:
            self._module.shutdown()
            self._module = None


@register_adapter("ros2")
class Ros2ToolAdapter:
    """按工具名分派到 service / action 的 ROS2 适配器。"""

    name = "ros2"

    def __init__(self, config: dict[str, Any] | None = None, *, backend: Any | None = None) -> None:
        self._tools = dict((config or {}).get("tools", {}))
        self._backend = backend or RclpyBackend()

    async def execute(self, call: ToolCall, cancel: CancellationToken) -> ToolResult:
        spec = self._tools.get(call.name)
        if spec is None:
            return ToolResult(
                ok=False,
                error=f"ROS2 适配器未配置工具 {call.name}",
                error_kind="adapter",
            )
        if cancel.cancelled:
            return ToolResult(ok=False, error="执行被取消", error_kind="cancelled")
        target = str(spec.get("target", ""))
        options = dict(spec.get("options") or {})
        kind = str(options.get("kind", "service"))
        if not target:
            return ToolResult(
                ok=False,
                error=f"工具 {call.name} 缺少目标地址",
                error_kind="adapter",
            )
        try:
            if kind == "action":
                raw = await self._backend.send_action(target, call.args)
            else:
                raw = await self._backend.call_service(target, call.args)
        except Exception as exc:
            logger.warning("ros2_tool_failed", tool=call.name, target=target, detail=repr(exc))
            return ToolResult(ok=False, error=repr(exc), error_kind="adapter")
        return ToolResult(ok=True, output=_stringify(raw))

    async def health(self) -> HealthStatus:
        module = getattr(self._backend, "_module", None)
        if module is None:
            return HealthStatus.ok("后端待首次调用初始化")
        return HealthStatus.ok(f"ROS2 工具 {len(self._tools)} 个")

    async def aclose(self) -> None:
        close = getattr(self._backend, "aclose", None)
        if close is not None:
            await close()


def _stringify(raw: Any) -> str:
    if raw is None:
        return "执行成功"
    if isinstance(raw, str):
        return raw
    message = getattr(raw, "message", None)
    if isinstance(message, str) and message:
        return message
    success = getattr(raw, "success", None)
    if isinstance(success, bool):
        return "执行成功" if success else "执行失败"
    return str(raw)


__all__ = ["RclpyBackend", "Ros2Backend", "Ros2ToolAdapter"]
