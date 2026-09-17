"""Cooperative runner cancellation driven by committed conversation turn state."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models.agent_conversation import AgentConversationTurn, AgentConversationTurnStatus
from backend.ws_agent_manager import AgentTaskUnresolvedError

CANCEL_REQUESTED = "cancel_requested"
CANCELLED = "cancelled"
CANCELLED_MESSAGE = "已停止当前回复；已完成的操作不会撤销。"
POLL_INTERVAL_SECONDS = 0.2
_active_turns: set[str] = set()


def register_turn_runner(turn_id: str) -> None:
    _active_turns.add(turn_id)


def unregister_turn_runner(turn_id: str) -> None:
    _active_turns.discard(turn_id)


def has_turn_runner(turn_id: str) -> bool:
    return turn_id in _active_turns


class ConversationTurnCancelledError(Exception):
    """The pending runner has acknowledged cancellation and finished cleanup."""


async def await_cleanup[Result](
    coroutine: Coroutine[Any, Any, Result], *, propagate_cancellation: bool = True
) -> Result:
    """Finish owned cleanup even if the parent receives repeated cancellation."""
    cleanup = asyncio.create_task(coroutine)
    interrupted = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            interrupted = True
    result = cleanup.result()
    if interrupted and propagate_cancellation:
        raise asyncio.CancelledError
    return result


async def _watch_cancellation(
    sessions: async_sessionmaker[AsyncSession],
    *,
    conversation_id: str,
    workspace_id: str,
    turn_id: str,
    is_disconnected: Callable[[], Awaitable[bool]] | None,
) -> None:
    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        if is_disconnected is not None and await is_disconnected():
            return
        async with sessions() as polling_db:
            requested = await polling_db.scalar(
                select(AgentConversationTurn.id).where(
                    AgentConversationTurn.id == turn_id,
                    AgentConversationTurn.conversation_id == conversation_id,
                    AgentConversationTurn.workspace_id == workspace_id,
                    AgentConversationTurn.status == AgentConversationTurnStatus.RUNNING.value,
                    AgentConversationTurn.error_code == CANCEL_REQUESTED,
                )
            )
        if requested is not None:
            return


async def _drain_tasks(*tasks: asyncio.Task) -> None:
    for task in tasks:
        if not task.done() and not task.cancelling():
            task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, AgentTaskUnresolvedError):
            raise result


async def run_with_cancellation[Result](
    coroutine: Coroutine[Any, Any, Result],
    sessions: async_sessionmaker[AsyncSession],
    *,
    conversation_id: str,
    workspace_id: str,
    turn_id: str,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> Result:
    runner = asyncio.create_task(coroutine)
    watcher = asyncio.create_task(
        _watch_cancellation(
            sessions,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            turn_id=turn_id,
            is_disconnected=is_disconnected,
        )
    )
    try:
        await asyncio.wait((runner, watcher), return_when=asyncio.FIRST_COMPLETED)
        if runner.done():
            return runner.result()
        watcher.result()
        runner.cancel()
        try:
            return await asyncio.shield(runner)
        except asyncio.CancelledError as exc:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            raise ConversationTurnCancelledError from exc
    finally:
        await await_cleanup(_drain_tasks(runner, watcher))
