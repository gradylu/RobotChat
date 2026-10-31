"""T-3.3 取消令牌单测：父子传播、幂等、可 await 收敛（INV-5）。"""

from __future__ import annotations

import asyncio

from robotchat.core.cancel import CancellationToken


async def test_parent_cancel_propagates_to_children() -> None:
    root = CancellationToken("root")
    first = root.child("first")
    second = root.child("second")
    grandchild = first.child("grandchild")

    root.cancel("user")

    assert root.cancelled and first.cancelled and second.cancelled
    assert grandchild.cancelled
    assert first.reason == "user"
    await asyncio.wait_for(grandchild.wait(), timeout=0.1)


async def test_cancel_is_idempotent_and_keeps_first_reason() -> None:
    token = CancellationToken("token")
    token.cancel("first")
    token.cancel("second")

    assert token.reason == "first"
    assert token.cancelled is True


async def test_child_created_after_cancel_is_already_cancelled() -> None:
    root = CancellationToken("root")
    root.cancel("closed")

    late = root.child("late")
    assert late.cancelled
    assert late.reason == "closed"


async def test_bind_task_and_settle() -> None:
    token = CancellationToken("token")

    async def worker() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(worker())
    token.bind_task(task)
    token.cancel("abort")

    assert await token.settle(timeout=0.5) is True
    assert task.cancelled()


async def test_settle_without_tasks_is_immediate() -> None:
    token = CancellationToken("token")
    assert await token.settle(timeout=0.1) is True
