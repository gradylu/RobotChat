"""OpenAI 兼容 LLM 统一驱动：覆盖 vLLM / Ollama / 云端 API（T-2.3、T-2.11）。

- 流式 SSE 解析，逐 token 产出 `LLMEvent`；
- 支持 tool call 增量拼装，结束时产出结构化 `tool_call`；
- 按地址位置分档设置超时（本机 / 边端 / 云端），仅改 `base_url` 即可切换部署位置；
- 本机地址默认拒绝，须显式 `allow_local_llm=true`（拓扑 2）且资源校验通过。
"""

from __future__ import annotations

import ipaddress
import json
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from robotchat.core.cancel import CancellationToken
from robotchat.health import HealthStatus
from robotchat.observability.logging import get_logger
from robotchat.providers.base import ChatRequest, LLMCapabilities, LLMEvent
from robotchat.providers.registry import register_provider

logger = get_logger("robotchat.providers.llm.openai_compat")

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
_TIER_TIMEOUTS: dict[str, dict[str, int]] = {
    # 单位毫秒；C-41 要求边端档与公网档首 token 均 ≤ 800ms
    "local": {"connect": 300, "first_token": 800, "total": 30000},
    "edge": {"connect": 300, "first_token": 800, "total": 30000},
    "cloud": {"connect": 500, "first_token": 800, "total": 60000},
}


def _has_placeholder(value: str) -> bool:
    return "${" in value


def endpoint_tier(host: str) -> str:
    """按地址位置分档：loopback → local；私网 → edge；其余 → cloud。"""
    if host in _LOCAL_HOSTS:
        return "local"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "cloud"
    if address.is_loopback:
        return "local"
    if address.is_private:
        return "edge"
    return "cloud"


def available_memory_gb() -> float | None:
    """读取 Linux 可用内存；非 Linux 或不可读时返回 None（跳过校验）。"""
    path = Path("/proc/meminfo")
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return float(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return None
    return None


def _resource_check(min_memory_gb: float) -> tuple[bool, str]:
    available = available_memory_gb()
    if available is None:
        return True, "无法读取本机内存信息（非 Linux 环境），跳过资源校验"
    if available < min_memory_gb:
        return False, f"本机可用内存 {available:.1f}GB 低于拓扑 2 下限 {min_memory_gb:.1f}GB"
    return True, f"本机可用内存 {available:.1f}GB 满足拓扑 2 下限"


@register_provider("llm", "openai_compat")
@register_provider("llm", "edge_llm")
@register_provider("llm", "openai_cloud")
@register_provider("llm", "vllm")
@register_provider("llm", "ollama")
class OpenAICompatLLM:
    name = "openai_compat"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        cfg = config or {}
        base_url = str(cfg.get("base_url", "") or "").strip()
        if not base_url or _has_placeholder(base_url):
            raise ValueError(
                "LLM base_url 未配置：请设置环境变量 LLM_BASE_URL 或 "
                "providers.llm.drivers.*.base_url"
            )
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"LLM base_url 非法：{base_url!r}（须为 http(s) 地址）")
        self.base_url = base_url.rstrip("/")
        model = str(cfg.get("model", "") or "").strip()
        if not model or _has_placeholder(model):
            raise ValueError("LLM model 未配置：请设置环境变量 LLM_MODEL 或 drivers.*.model")
        self.model = model
        self.tier = endpoint_tier(parsed.hostname)
        allow_local = bool(cfg.get("allow_local_llm", False))
        if self.tier == "local":
            if not allow_local:
                raise ValueError(
                    "检测到本机 LLM 地址。默认拓扑要求 LLM 在边端 / 云端（CON-12、A5）；"
                    "如确为拓扑 2，请显式设置 allow_local_llm=true"
                )
            min_memory_gb = float(cfg.get("local_llm_min_memory_gb", 8.0) or 8.0)
            ok, detail = _resource_check(min_memory_gb)
            if not ok:
                raise ValueError(f"本机 LLM 资源校验未通过：{detail}（拓扑 2 须重新核算资源）")
            logger.warning("local_llm_allowed", detail=detail, base_url=self.base_url)
        self.supports_tools = bool(cfg.get("supports_tools", True))
        self.capabilities = LLMCapabilities(
            streaming=bool(cfg.get("stream", True)),
            tool_call=self.supports_tools,
            context_window=int(cfg.get("context_window", 8192) or 8192),
        )
        self.api_key_env = str(cfg.get("api_key_env", "") or "")
        self.health_path = str(cfg.get("health_path", "/models") or "")
        timeout_cfg = dict(_TIER_TIMEOUTS[self.tier])
        timeout_cfg.update({str(k): int(v) for k, v in (cfg.get("timeout_ms") or {}).items()})
        self.timeouts = timeout_cfg
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------ HTTP

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self.api_key_env, "") if self.api_key_env else ""
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            self.timeouts["total"] / 1000.0,
            connect=self.timeouts["connect"] / 1000.0,
            read=self.timeouts["first_token"] / 1000.0,
            write=10.0,
        )

    async def health(self) -> HealthStatus:
        started = time.monotonic()
        try:
            response = await self._http().get(
                self.base_url + self.health_path,
                headers=self._headers(),
                timeout=httpx.Timeout(1.0, connect=self.timeouts["connect"] / 1000.0),
            )
        except Exception as exc:
            return HealthStatus.unavailable(repr(exc))
        latency = round((time.monotonic() - started) * 1000.0, 2)
        if response.status_code >= 500:
            return HealthStatus.unavailable(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            return HealthStatus.degraded(f"HTTP {response.status_code}")
        return HealthStatus.ok(f"HTTP {response.status_code}", latency_ms=latency)

    async def warmup(self) -> None:
        status = await self.health()
        if status.state == "unavailable":
            raise RuntimeError(status.detail or "LLM 服务不可达")

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    # ------------------------------------------------------------ 对话

    def _payload(self, req: ChatRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": item.role, "content": item.content} for item in req.messages],
            "stream": True,
        }
        if req.tools and self.capabilities.tool_call:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters or {"type": "object", "properties": {}},
                    },
                }
                for tool in req.tools
            ]
            payload["tool_choice"] = "auto"
        return payload

    async def chat_stream(
        self, req: ChatRequest, cancel: CancellationToken
    ) -> AsyncIterator[LLMEvent]:
        payload = self._payload(req)
        url = self.base_url + "/chat/completions"
        tool_buffers: dict[int, dict[str, str]] = {}
        started = time.monotonic()
        try:
            async with self._http().stream(
                "POST", url, json=payload, headers=self._headers(), timeout=self._timeout()
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:200]
                    yield LLMEvent(type="error", error=f"HTTP {response.status_code}: {body}")
                    return
                async for line in response.aiter_lines():
                    if cancel.cancelled:
                        return
                    elapsed = (time.monotonic() - started) * 1000.0
                    if elapsed > self.timeouts["total"]:
                        yield LLMEvent(type="error", error="LLM 总时长超限")
                        return
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for choice in chunk.get("choices") or ():
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            yield LLMEvent(type="token", text=content)
                        for call in delta.get("tool_calls") or ():
                            _accumulate_tool_call(tool_buffers, call)
        except httpx.HTTPError as exc:
            yield LLMEvent(type="error", error=repr(exc))
            return
        except Exception as exc:  # 解析或连接异常统一转为 error 事件
            yield LLMEvent(type="error", error=repr(exc))
            return

        for index in sorted(tool_buffers):
            buffer = tool_buffers[index]
            if not buffer.get("name"):
                continue
            arguments = buffer.get("arguments", "")
            try:
                parsed_args = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                parsed_args = {"_raw": arguments}
            yield LLMEvent(
                type="tool_call",
                tool_call={
                    "id": buffer.get("id") or f"call_{index}",
                    "name": buffer["name"],
                    "arguments": parsed_args,
                },
            )
        yield LLMEvent(type="done")


def _accumulate_tool_call(buffers: dict[int, dict[str, str]], call: dict[str, Any]) -> None:
    index = int(call.get("index", 0) or 0)
    buffer = buffers.setdefault(index, {"id": "", "name": "", "arguments": ""})
    if call.get("id"):
        buffer["id"] = str(call["id"])
    function = call.get("function") or {}
    if function.get("name"):
        buffer["name"] = str(function["name"])
    if function.get("arguments"):
        buffer["arguments"] += str(function["arguments"])


__all__ = ["OpenAICompatLLM", "endpoint_tier"]
