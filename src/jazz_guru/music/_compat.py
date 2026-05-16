"""Async/sync interop helpers used by the music adapters.

The adapter protocols are synchronous (``transcribe_to_midi``,
``generate_audio``, ...). Some adapters internally drive asyncio
subprocesses (``asyncio.create_subprocess_exec``) to talk to a CLI.
Calling :func:`asyncio.run` from those sync methods crashes when the
method is invoked from an existing event loop (the agent orchestrator
puts adapters on a thread, but other callers — tests, future code — may
not). :func:`run_coro_sync` bridges the gap safely: it uses
:func:`asyncio.run` when no loop is running, and offloads to a fresh
thread+loop when one is.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Coroutine
from typing import Any


def run_coro_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive ``coro`` to completion regardless of the caller's asyncio state.

    * If no event loop is running, this is equivalent to
      ``asyncio.run(coro)``.
    * If a loop is running, the coroutine is dispatched to a one-shot
      ``ThreadPoolExecutor`` worker that owns its own loop, so we never
      raise ``RuntimeError: asyncio.run() cannot be called from a
      running event loop``.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(asyncio.run, coro)
        return future.result()
