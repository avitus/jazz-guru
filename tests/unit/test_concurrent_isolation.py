"""Verify per-async-task isolation of dynamic registry + meta event sink.

Two concurrent tasks each bind their own dynamic registry and event sink,
then await across a yield point. Each must see *only* its own bindings,
not the other task's. Before the ContextVar refactor, both bindings were
module-global and would clobber each other.
"""

from __future__ import annotations

import asyncio

from jazz_guru.actions.dynamic import DynamicRegistry, DynamicSpec, hash_source
from jazz_guru.actions.registry import registry
from jazz_guru.actions.tools import tool_meta


def _spec(name: str) -> DynamicSpec:
    src = "def run(**kw):\n    return {'ok': True}\n"
    return DynamicSpec(
        name=name,
        description=f"test tool {name}",
        input_schema={"type": "object"},
        source=src,
        sha256=hash_source(src),
    )


async def test_dynamic_overlay_isolated_across_tasks() -> None:
    # Use a unique prefix that won't collide with the static meta-tool
    # names (tool_create, tool_publish, …) registered in this process.
    PREFIX = "isotest_"

    async def task(label: str) -> list[str]:
        dyn = DynamicRegistry()
        dyn.add(_spec(f"{PREFIX}{label}"))
        tok = registry.attach_dynamic(dyn)
        try:
            await asyncio.sleep(0.01)  # let the other task interleave
            names_seen = registry.names()
        finally:
            registry.detach_dynamic(tok)
        return [n for n in names_seen if n.startswith(PREFIX)]

    a, b = await asyncio.gather(task("a"), task("b"))
    assert a == [f"{PREFIX}a"]
    assert b == [f"{PREFIX}b"]
    # After both tasks return, the overlay is fully detached.
    assert registry.current_dynamic() is None


async def test_meta_event_sink_isolated_across_tasks() -> None:
    async def task(label: str) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        tok = tool_meta.set_event_sink(lambda n, p: events.append((label, n)))
        try:
            await asyncio.sleep(0.01)
            tool_meta._emit(f"evt_{label}", {})
        finally:
            tool_meta.reset_event_sink(tok)
        return events

    a, b = await asyncio.gather(task("a"), task("b"))
    # Each task only saw events emitted under its own bound sink.
    assert a == [("a", "evt_a")]
    assert b == [("b", "evt_b")]
