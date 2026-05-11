"""Confirms the ActionController forwards stream deltas to ``on_event``.

This is the wiring guard: complete() emits deltas via on_delta, the controller
wraps them as ``llm_delta`` events. The TUI/WS server already subscribes to
controller events, so this is what they'll observe.
"""

from __future__ import annotations

from typing import Any

import pytest

from jazz_guru.actions import controller as ctrl_mod
from jazz_guru.actions.controller import ActionController
from jazz_guru.llm import LLMResponse, LLMUsage


async def test_controller_emits_llm_delta_events(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_events: list[tuple[str, dict[str, Any]]] = []

    def _on_event(name: str, payload: dict[str, Any]) -> None:
        captured_events.append((name, payload))

    async def _fake_complete(
        _messages: list[dict[str, Any]],
        *,
        on_delta: Any = None,
        **_kw: Any,
    ) -> LLMResponse:
        # Simulate the streaming path: complete() would call on_delta for each
        # incremental event before returning the assembled message.
        if on_delta is not None:
            on_delta({"type": "text", "index": 0, "text": "hel"})
            on_delta({"type": "text", "index": 0, "text": "lo"})
        return LLMResponse(
            raw=None,
            text="hello",
            tool_uses=[],
            stop_reason="end_turn",
            usage=LLMUsage(input_tokens=1, output_tokens=2),
        )

    monkeypatch.setattr(ctrl_mod, "complete", _fake_complete)

    c = ActionController(on_event=_on_event)
    await c.run(system="sys", messages=[{"role": "user", "content": "hi"}])

    deltas = [p for (n, p) in captured_events if n == "llm_delta"]
    assert deltas == [
        {"round": 0, "type": "text", "index": 0, "text": "hel"},
        {"round": 0, "type": "text", "index": 0, "text": "lo"},
    ]
    # Sanity: existing event contract still holds.
    assert any(n == "llm_request" for n, _ in captured_events)
    assert any(n == "llm_response" for n, _ in captured_events)


async def test_controller_tags_delta_with_correct_round(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each round's deltas must carry that round's index — even across
    successive iterations of the tool_use loop."""
    captured: list[tuple[str, dict[str, Any]]] = []
    state = {"round": 0}

    async def _fake_complete(
        _messages: list[dict[str, Any]],
        *,
        on_delta: Any = None,
        **_kw: Any,
    ) -> LLMResponse:
        if on_delta is not None:
            on_delta({"type": "text", "index": 0, "text": f"r{state['round']}"})
        # First call -> tool_use to force a second round; second -> end_turn.
        if state["round"] == 0:
            state["round"] = 1
            return LLMResponse(
                raw=None,
                text="",
                tool_uses=[{"id": "t1", "name": "shell", "input": {"cmd": "echo ok"}}],
                stop_reason="tool_use",
                usage=LLMUsage(input_tokens=1, output_tokens=1),
            )
        return LLMResponse(
            raw=None, text="done", tool_uses=[], stop_reason="end_turn",
            usage=LLMUsage(input_tokens=1, output_tokens=1),
        )

    async def _fake_invoke(_name: str, _inp: dict[str, Any]) -> Any:
        return {"ok": True}

    monkeypatch.setattr(ctrl_mod, "complete", _fake_complete)

    c = ActionController(on_event=lambda n, p: captured.append((n, p)))
    # Allow the synthetic shell tool by stubbing registry.invoke (the real
    # tool is policy-allowed by default but we don't want it to run shell).
    monkeypatch.setattr(c.registry, "invoke", _fake_invoke)
    # Also make sure the synthetic tool name passes the allowlist filter,
    # which reads policy + registry. Easiest: stub _allowed_set.
    monkeypatch.setattr(c, "_allowed_set", lambda: {"shell"})

    await c.run(system="sys", messages=[{"role": "user", "content": "hi"}])

    deltas = [p for n, p in captured if n == "llm_delta"]
    assert [d["round"] for d in deltas] == [0, 1]
    assert [d["text"] for d in deltas] == ["r0", "r1"]
