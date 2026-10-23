"""Let a cancel request abort a read-only wait (a model call) instead of waiting for it to return.

A cancelled run used to stop only "at the next safe point", i.e. after the model call in flight came back, which can take
minutes when the provider is overloaded. Model calls only read, so abandoning one is always safe; tool calls that may write are
never wrapped in this.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")
ABORT_GRACE_S = 2.0  # how long an aborted call may take to unwind before it is abandoned in the background


class CallInterrupted(Exception):
    """The awaited call was abandoned because the cancel event was set."""


async def await_unless_set(awaitable: Awaitable[T], cancelled: asyncio.Event | None) -> T:
    """Await ``awaitable``; raise ``CallInterrupted`` as soon as ``cancelled`` is set (the call is cancelled, not awaited).

    A call that already finished wins over a cancel that arrives at the same moment, so no result is thrown away. An
    event that is set before the call starts means it is never started. Cancelling the caller cancels the call too."""
    if cancelled is None:
        return await awaitable
    if cancelled.is_set():
        if asyncio.iscoroutine(awaitable):
            awaitable.close()  # never started: avoid "coroutine was never awaited"
        raise CallInterrupted()
    call = asyncio.ensure_future(awaitable)
    watcher = asyncio.ensure_future(cancelled.wait())
    try:
        await asyncio.wait({call, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if call.done():
            return call.result()
        raise CallInterrupted()
    finally:
        watcher.cancel()
        if not call.done():
            call.cancel()
            call.add_done_callback(_consume)  # an error raised while unwinding must not be logged as "never retrieved"
            await asyncio.wait({call}, timeout=ABORT_GRACE_S)


def _consume(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()
