"""层级取消令牌（T-3.3、INV-5）。

- 父令牌取消时递归传播到全部子令牌；
- `cancel()` 幂等：重复取消不报错、不覆盖首次原因；
- 子任务可通过 `bind_task()` 注册，取消时同步取消，`settle()` 可 await 收敛；
- 在父取消之后创建的子令牌立即处于已取消状态。
"""

from __future__ import annotations

import asyncio
from typing import Any


class CancellationToken:
    """可组合的取消信号；下游任务只读 `cancelled` 并 `await wait()`。"""

    def __init__(self, name: str = "root", parent: CancellationToken | None = None) -> None:
        self.name = name
        self._parent = parent
        self._cancelled = False
        self._reason = ""
        self._event = asyncio.Event()
        self._children: list[CancellationToken] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        if parent is not None:
            parent._add_child(self)

    # ------------------------------------------------------------ 只读状态

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str:
        return self._reason

    # ------------------------------------------------------------ 取消

    def cancel(self, reason: str = "") -> None:
        """幂等取消，并传播到子令牌与已注册任务。"""
        if self._cancelled:
            return
        self._cancelled = True
        self._reason = reason
        self._event.set()
        for child in tuple(self._children):
            child.cancel(reason)
        for task in tuple(self._tasks):
            if not task.done():
                task.cancel()

    async def wait(self) -> None:
        """挂起直到取消；已取消时立即返回。"""
        await self._event.wait()

    # ------------------------------------------------------------ 组合

    def child(self, name: str = "child") -> CancellationToken:
        return CancellationToken(name=name, parent=self)

    def bind_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """把任务注册到令牌；取消令牌时任务被取消（已结束的任务自动跳过）。"""
        if self._cancelled:
            task.cancel()
            return task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def settle(self, timeout: float | None = None) -> bool:
        """等待子树内已注册任务全部收敛；超时返回 False（任务仍在运行）。"""
        tasks = [
            task
            for task in self._all_tasks()
            if not task.done() and task is not asyncio.current_task()
        ]
        if not tasks:
            return True
        await asyncio.wait(tasks, timeout=timeout)
        return all(task.done() for task in tasks)

    # ------------------------------------------------------------ 内部

    def _add_child(self, child: CancellationToken) -> None:
        if self._cancelled:
            child.cancel(self._reason)
            return
        self._children.append(child)

    def _all_tasks(self) -> set[asyncio.Task[Any]]:
        tasks = set(self._tasks)
        for child in self._children:
            tasks |= child._all_tasks()
        return tasks


__all__ = ["CancellationToken"]
