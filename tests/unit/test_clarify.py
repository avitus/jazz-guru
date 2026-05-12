"""Tests for the clarify tool + controller pause/resume flow."""
from __future__ import annotations

from typing import Any

import pytest

from jazz_guru.actions import controller as ctrl_mod
from jazz_guru.actions.controller import ActionController
from jazz_guru.actions.registry import register_all, registry
from jazz_guru.llm import LLMResponse, LLMUsage


async def test_clarify_tool_returns_sentinel() -> None:
    register_all()
    out = await registry.invoke(
        "clarify",
        {
            "question": "Which key?",
            "options": ["F", "Bb", "Eb"],
        },
    )
    assert "__clarify__" in out
    payload = out["__clarify__"]
    assert payload["question"] == "Which key?"
    assert payload["options"] == ["F", "Bb", "Eb"]
    assert payload["multi"] is False


async def test_clarify_tool_passes_multi_and_header() -> None:
    register_all()
    out = await registry.invoke(
        "clarify",
        {
            "question": "Pick instruments",
            "options": ["sax", "trumpet", "trombone"],
            "multi": True,
            "header": "Horns",
        },
    )
    p = out["__clarify__"]
    assert p["multi"] is True
    assert p["header"] == "Horns"


async def test_controller_intercepts_clarify_with_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a clarify_callback is bound, the controller substitutes the
    callback's answer as the tool_result and emits clarify_request/response.
    """
    captured: list[tuple[str, dict[str, Any]]] = []
    state = {"round": 0}

    async def _fake_complete(
        _messages: list[dict[str, Any]], *, on_delta: Any = None, **_kw: Any
    ) -> LLMResponse:
        # Round 0: model asks for clarification via the clarify tool.
        # Round 1: model emits end_turn after seeing the user's answer.
        if state["round"] == 0:
            state["round"] = 1
            return LLMResponse(
                raw=None,
                text="",
                tool_uses=[
                    {
                        "id": "clar1",
                        "name": "clarify",
                        "input": {"question": "Which key?", "options": ["F", "Bb"]},
                    }
                ],
                stop_reason="tool_use",
                usage=LLMUsage(input_tokens=1, output_tokens=1),
            )
        return LLMResponse(
            raw=None,
            text="Got it, going with Bb",
            tool_uses=[],
            stop_reason="end_turn",
            usage=LLMUsage(input_tokens=1, output_tokens=1),
        )

    callback_calls: list[dict[str, Any]] = []

    async def _callback(payload: dict[str, Any]) -> str:
        callback_calls.append(payload)
        return "Bb"

    monkeypatch.setattr(ctrl_mod, "complete", _fake_complete)
    c = ActionController(
        on_event=lambda n, p: captured.append((n, p)),
        clarify_callback=_callback,
    )
    monkeypatch.setattr(c, "_allowed_set", lambda: {"clarify"})

    res = await c.run(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert res.final_text == "Got it, going with Bb"
    assert len(callback_calls) == 1
    assert callback_calls[0]["question"] == "Which key?"

    # Verify the event sequence
    names = [n for n, _ in captured]
    assert "clarify_request" in names
    assert "clarify_response" in names
    # The tool_use_id from clarify_request should match the assistant's tool_use id
    req = next(p for n, p in captured if n == "clarify_request")
    assert req["tool_use_id"] == "clar1"
    resp = next(p for n, p in captured if n == "clarify_response")
    assert resp["answer"] == "Bb"


async def test_controller_passes_through_without_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no callback, the sentinel is forwarded to the model as-is."""
    state = {"round": 0}

    async def _fake_complete(
        _messages: list[dict[str, Any]], *, on_delta: Any = None, **_kw: Any
    ) -> LLMResponse:
        if state["round"] == 0:
            state["round"] = 1
            return LLMResponse(
                raw=None,
                text="",
                tool_uses=[
                    {
                        "id": "clar1",
                        "name": "clarify",
                        "input": {"question": "x?"},
                    }
                ],
                stop_reason="tool_use",
                usage=LLMUsage(input_tokens=1, output_tokens=1),
            )
        return LLMResponse(
            raw=None,
            text="proceeding by judgment",
            tool_uses=[],
            stop_reason="end_turn",
            usage=LLMUsage(input_tokens=1, output_tokens=1),
        )

    monkeypatch.setattr(ctrl_mod, "complete", _fake_complete)
    c = ActionController(clarify_callback=None)
    monkeypatch.setattr(c, "_allowed_set", lambda: {"clarify"})
    res = await c.run(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert "proceeding" in res.final_text
    # Inspect the assistant + tool_result messages — the tool_result should
    # contain the sentinel JSON.
    tool_result_msg = next(
        m for m in res.messages if m.get("role") == "user" and isinstance(m["content"], list)
    )
    block = tool_result_msg["content"][0]
    assert block["type"] == "tool_result"
    text_content = block["content"][0]["text"]
    assert "__clarify__" in text_content


async def test_controller_surfaces_callback_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception in the clarify callback becomes an is_error tool_result."""
    state = {"round": 0}
    captured: list[tuple[str, dict[str, Any]]] = []

    async def _fake_complete(
        _messages: list[dict[str, Any]], *, on_delta: Any = None, **_kw: Any
    ) -> LLMResponse:
        if state["round"] == 0:
            state["round"] = 1
            return LLMResponse(
                raw=None,
                text="",
                tool_uses=[
                    {
                        "id": "clar1",
                        "name": "clarify",
                        "input": {"question": "x?"},
                    }
                ],
                stop_reason="tool_use",
                usage=LLMUsage(input_tokens=1, output_tokens=1),
            )
        return LLMResponse(
            raw=None,
            text="done",
            tool_uses=[],
            stop_reason="end_turn",
            usage=LLMUsage(input_tokens=1, output_tokens=1),
        )

    async def _bad_callback(_payload: dict[str, Any]) -> str:
        raise RuntimeError("ws disconnected")

    monkeypatch.setattr(ctrl_mod, "complete", _fake_complete)
    c = ActionController(
        on_event=lambda n, p: captured.append((n, p)),
        clarify_callback=_bad_callback,
    )
    monkeypatch.setattr(c, "_allowed_set", lambda: {"clarify"})
    res = await c.run(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert any("clarify_callback_failed" in e for e in res.errors)
    # The model still got an is_error tool_result and can continue.
    err_events = [
        p for n, p in captured if n == "tool_result" and not p.get("ok", True)
    ]
    assert err_events
