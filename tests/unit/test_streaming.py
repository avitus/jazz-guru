from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from jazz_guru import llm
from jazz_guru.actions.context import ToolContext, reset_tool_context, set_tool_context
from jazz_guru.actions.controller import ActionController, RunResult
from jazz_guru.actions.registry import ToolRegistry
from jazz_guru.config import Policy, ToolPolicy, get_settings
from jazz_guru.llm import LLMResponse, LLMUsage, complete_stream


@dataclass
class _FakeText:
    type: str
    text: str


@dataclass
class _FakeToolUse:
    type: str
    id: str
    name: str
    input: dict[str, Any]


class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeMessage:
    def __init__(
        self,
        text: str,
        tool_uses: list[_FakeToolUse] | None = None,
        stop_reason: str = "end_turn",
    ) -> None:
        content: list[Any] = []
        if text:
            content.append(_FakeText(type="text", text=text))
        if tool_uses:
            content.extend(tool_uses)
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _FakeUsage(input_tokens=10, output_tokens=20)


class _FakeStream:
    """Mimics the AsyncMessageStream context manager used by complete_stream."""

    def __init__(self, deltas: list[str], final: _FakeMessage) -> None:
        self._deltas = deltas
        self._final = final

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            for d in self._deltas:
                yield SimpleNamespace(type="text", text=d)

        return gen()

    async def get_final_message(self) -> _FakeMessage:
        return self._final


@pytest.fixture
def patched_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    state: dict[str, Any] = {"deltas": ["hel", "lo"], "final": _FakeMessage("hello")}

    def fake_stream(**kwargs: Any) -> _FakeStream:
        captured["kwargs"] = kwargs
        return _FakeStream(state["deltas"], state["final"])

    fake_messages = SimpleNamespace(stream=fake_stream)
    fake_client = SimpleNamespace(messages=fake_messages)
    monkeypatch.setattr(llm, "get_client", lambda: fake_client)
    return {"captured": captured, "state": state}


async def test_complete_stream_yields_deltas_then_done(patched_client: dict[str, Any]) -> None:
    events: list[dict[str, Any]] = []
    async for evt in complete_stream(messages=[{"role": "user", "content": "hi"}]):
        events.append(evt)
    assert [e["type"] for e in events] == ["text_delta", "text_delta", "done"]
    assert events[0]["delta"] == "hel"
    assert events[1]["delta"] == "lo"
    resp = events[2]["response"]
    assert isinstance(resp, LLMResponse)
    assert resp.text == "hello"
    assert resp.stop_reason == "end_turn"
    assert resp.usage.input_tokens == 10
    assert resp.usage.output_tokens == 20


async def test_complete_stream_passes_args_to_sdk(patched_client: dict[str, Any]) -> None:
    async for _ in complete_stream(
        messages=[{"role": "user", "content": "hi"}],
        system="be terse",
        tools=[{"name": "noop", "description": "x", "input_schema": {"type": "object"}}],
        max_tokens=99,
        temperature=0.1,
        model="claude-test",
    ):
        pass
    kw = patched_client["captured"]["kwargs"]
    assert kw["model"] == "claude-test"
    assert kw["max_tokens"] == 99
    assert kw["temperature"] == 0.1
    assert kw["system"] == "be terse"
    assert kw["tools"][0]["name"] == "noop"


async def test_controller_emits_text_deltas_and_invokes_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive ActionController.run against a stub complete_stream that yields
    text + a single tool_use, then a final end_turn."""
    calls = {"count": 0}

    async def fake_complete_stream(
        messages: list[dict[str, Any]], **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        calls["count"] += 1
        if calls["count"] == 1:
            yield {"type": "text_delta", "delta": "thinking..."}
            yield {
                "type": "done",
                "response": LLMResponse(
                    raw=None,
                    text="thinking...",
                    tool_uses=[{"id": "tu_1", "name": "noop", "input": {"x": 1}}],
                    stop_reason="tool_use",
                    usage=LLMUsage(input_tokens=5, output_tokens=2),
                ),
            }
        else:
            for chunk in ("done", "!"):
                yield {"type": "text_delta", "delta": chunk}
            yield {
                "type": "done",
                "response": LLMResponse(
                    raw=None,
                    text="done!",
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=LLMUsage(input_tokens=3, output_tokens=2),
                ),
            }

    # Patch the controller's import of complete_stream.
    import jazz_guru.actions.controller as ctl

    monkeypatch.setattr(ctl, "complete_stream", fake_complete_stream)

    # Build a tiny registry with one fake tool.
    reg = ToolRegistry()

    async def noop_handler(x: int = 0) -> dict[str, int]:
        return {"echo": x}

    class _Inp(BaseModel):
        x: int = 0

    reg.register("noop", description="noop", input_model=_Inp)(noop_handler)

    policy = Policy(
        default="allow",
        tools={"noop": ToolPolicy(mode="allow")},
    )

    events: list[tuple[str, dict[str, Any]]] = []

    def on_event(name: str, payload: dict[str, Any]) -> None:
        events.append((name, payload))

    controller = ActionController(registry=reg, policy=policy, on_event=on_event)
    res: RunResult = await controller.run(
        system="x", messages=[{"role": "user", "content": "go"}]
    )

    # Two LLM rounds happened, the tool ran exactly once, final text matches.
    assert calls["count"] == 2
    assert res.tool_calls == 1
    assert res.final_text == "done!"
    # Three text_delta events were emitted ("thinking...", "done", "!").
    delta_events = [p for n, p in events if n == "text_delta"]
    assert len(delta_events) == 3
    assert [e["delta"] for e in delta_events] == ["thinking...", "done", "!"]
    # The first tool_result event is for our tool, with ok=True.
    tr_events = [p for n, p in events if n == "tool_result"]
    assert tr_events and tr_events[0]["ok"] is True


async def test_controller_pruning_emits_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When a tool returns a large blob, the tool_result event carries a manifest."""
    monkeypatch.setattr(get_settings(), "jg_workspace_dir", tmp_path)

    big = "z" * 50_000

    async def fake_complete_stream(
        messages: list[dict[str, Any]], **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        if len(messages) == 1:
            yield {
                "type": "done",
                "response": LLMResponse(
                    raw=None,
                    text="",
                    tool_uses=[{"id": "tu_1", "name": "blobber", "input": {}}],
                    stop_reason="tool_use",
                    usage=LLMUsage(),
                ),
            }
        else:
            yield {
                "type": "done",
                "response": LLMResponse(
                    raw=None,
                    text="ok",
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=LLMUsage(),
                ),
            }

    import jazz_guru.actions.controller as ctl

    monkeypatch.setattr(ctl, "complete_stream", fake_complete_stream)

    reg = ToolRegistry()

    async def blobber() -> dict[str, str]:
        return {"blob": big}

    class _BlobInp(BaseModel):
        pass

    reg.register("blobber", description="b", input_model=_BlobInp)(blobber)

    policy = Policy(
        default="allow",
        default_max_result_bytes=10_000,
        tools={"blobber": ToolPolicy(mode="allow")},
    )

    events: list[tuple[str, dict[str, Any]]] = []

    def on_event(name: str, payload: dict[str, Any]) -> None:
        events.append((name, payload))

    controller = ActionController(registry=reg, policy=policy, on_event=on_event)
    tok = set_tool_context(ToolContext(session_id="s1", turn_idx=0))
    try:
        await controller.run(system="x", messages=[{"role": "user", "content": "go"}])
    finally:
        reset_tool_context(tok)

    tr = [p for n, p in events if n == "tool_result"]
    assert tr, "expected at least one tool_result event"
    assert "manifest" in tr[0]
    assert tr[0]["manifest"]["tool"] == "blobber"
    assert tr[0]["manifest"]["size_bytes"] >= 50_000
