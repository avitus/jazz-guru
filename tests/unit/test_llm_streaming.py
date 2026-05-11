"""Behavioral tests for the streaming complete() path.

These tests run against an in-process fake Anthropic client. They verify:
  - on_delta receives text-delta and input_json-delta events
  - structural events are filtered out and don't reach on_delta
  - the final LLMResponse is assembled from get_final_message()
  - tool_use blocks survive the streaming round-trip
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from jazz_guru import llm as llm_mod

# ---------- minimal stream event + final-message fakes ----------


@dataclass
class _TextDelta:
    type: str
    text: str


@dataclass
class _JsonDelta:
    type: str
    partial_json: str


@dataclass
class _ContentBlockDeltaEvent:
    type: str
    index: int
    delta: Any


@dataclass
class _StructuralEvent:
    type: str  # e.g. "message_start" / "content_block_start" — no delta payload


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class _TextBlock:
    type: str
    text: str


@dataclass
class _ToolUseBlock:
    type: str
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class _FinalMessage:
    content: list[Any]
    stop_reason: str
    usage: _Usage


class _FakeStream:
    def __init__(self, events: list[Any], final: _FinalMessage) -> None:
        self._events = events
        self._final = final

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def __aiter__(self) -> _FakeStream:
        self._i = 0
        return self

    async def __anext__(self) -> Any:
        if self._i >= len(self._events):
            raise StopAsyncIteration
        ev = self._events[self._i]
        self._i += 1
        return ev

    async def get_final_message(self) -> _FinalMessage:
        return self._final


class _FakeMessages:
    def __init__(self, stream: _FakeStream) -> None:
        self._stream = stream

    def stream(self, **_kwargs: Any) -> _FakeStream:
        return self._stream


class _FakeClient:
    def __init__(self, stream: _FakeStream) -> None:
        self.messages = _FakeMessages(stream)


@pytest.fixture(autouse=True)
def _clear_client_cache() -> None:
    llm_mod.get_client.cache_clear()


def _install_fake(monkeypatch: pytest.MonkeyPatch, events: list[Any], final: _FinalMessage) -> None:
    stream = _FakeStream(events, final)
    monkeypatch.setattr(llm_mod, "get_client", lambda: _FakeClient(stream))


# ---------- tests ----------


async def test_complete_assembles_text_from_final_message(monkeypatch: pytest.MonkeyPatch) -> None:
    final = _FinalMessage(
        content=[_TextBlock(type="text", text="hello world")],
        stop_reason="end_turn",
        usage=_Usage(input_tokens=10, output_tokens=2),
    )
    _install_fake(monkeypatch, events=[], final=final)
    resp = await llm_mod.complete([{"role": "user", "content": "hi"}])
    assert resp.text == "hello world"
    assert resp.stop_reason == "end_turn"
    assert resp.usage.input_tokens == 10
    assert resp.usage.output_tokens == 2
    assert resp.tool_uses == []


async def test_complete_extracts_tool_uses(monkeypatch: pytest.MonkeyPatch) -> None:
    final = _FinalMessage(
        content=[
            _TextBlock(type="text", text="calling tool"),
            _ToolUseBlock(type="tool_use", id="t1", name="fs_write", input={"path": "x.txt"}),
        ],
        stop_reason="tool_use",
        usage=_Usage(input_tokens=5, output_tokens=8),
    )
    _install_fake(monkeypatch, events=[], final=final)
    resp = await llm_mod.complete([{"role": "user", "content": "go"}])
    assert resp.stop_reason == "tool_use"
    assert resp.text == "calling tool"
    assert resp.tool_uses == [{"id": "t1", "name": "fs_write", "input": {"path": "x.txt"}}]


async def test_on_delta_receives_text_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Any] = [
        _StructuralEvent(type="message_start"),
        _StructuralEvent(type="content_block_start"),
        _ContentBlockDeltaEvent(type="content_block_delta", index=0, delta=_TextDelta(type="text_delta", text="hel")),
        _ContentBlockDeltaEvent(type="content_block_delta", index=0, delta=_TextDelta(type="text_delta", text="lo")),
        _StructuralEvent(type="content_block_stop"),
    ]
    final = _FinalMessage(
        content=[_TextBlock(type="text", text="hello")],
        stop_reason="end_turn",
        usage=_Usage(input_tokens=1, output_tokens=2),
    )
    _install_fake(monkeypatch, events=events, final=final)

    seen: list[dict[str, Any]] = []
    await llm_mod.complete([{"role": "user", "content": "hi"}], on_delta=seen.append)
    assert seen == [
        {"type": "text", "index": 0, "text": "hel"},
        {"type": "text", "index": 0, "text": "lo"},
    ]


async def test_on_delta_receives_tool_input_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Any] = [
        _ContentBlockDeltaEvent(
            type="content_block_delta",
            index=1,
            delta=_JsonDelta(type="input_json_delta", partial_json='{"pa'),
        ),
        _ContentBlockDeltaEvent(
            type="content_block_delta",
            index=1,
            delta=_JsonDelta(type="input_json_delta", partial_json='th":"x"}'),
        ),
    ]
    final = _FinalMessage(
        content=[_ToolUseBlock(type="tool_use", id="t1", name="fs_write", input={"path": "x"})],
        stop_reason="tool_use",
        usage=_Usage(input_tokens=1, output_tokens=2),
    )
    _install_fake(monkeypatch, events=events, final=final)

    seen: list[dict[str, Any]] = []
    await llm_mod.complete([{"role": "user", "content": "go"}], on_delta=seen.append)
    assert seen == [
        {"type": "input_json", "index": 1, "partial_json": '{"pa'},
        {"type": "input_json", "index": 1, "partial_json": 'th":"x"}'},
    ]


async def test_on_delta_supports_async_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Any] = [
        _ContentBlockDeltaEvent(type="content_block_delta", index=0, delta=_TextDelta(type="text_delta", text="x"))
    ]
    final = _FinalMessage(
        content=[_TextBlock(type="text", text="x")],
        stop_reason="end_turn",
        usage=_Usage(input_tokens=1, output_tokens=1),
    )
    _install_fake(monkeypatch, events=events, final=final)

    seen: list[dict[str, Any]] = []

    async def _async_cb(p: dict[str, Any]) -> None:
        seen.append(p)

    await llm_mod.complete([{"role": "user", "content": "hi"}], on_delta=_async_cb)
    assert seen == [{"type": "text", "index": 0, "text": "x"}]


async def test_stream_not_iterated_when_on_delta_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skipping iteration is purely a performance optimisation, but we lock it
    in so the SDK's internal accumulator doesn't get duplicated work for
    callers that don't care about deltas."""

    class _PoisonStream(_FakeStream):
        async def __anext__(self) -> Any:
            raise AssertionError("stream should not be iterated when on_delta is None")

    stream = _PoisonStream(
        events=[],
        final=_FinalMessage(
            content=[_TextBlock(type="text", text="ok")],
            stop_reason="end_turn",
            usage=_Usage(input_tokens=1, output_tokens=1),
        ),
    )
    monkeypatch.setattr(llm_mod, "get_client", lambda: _FakeClient(stream))
    resp = await llm_mod.complete([{"role": "user", "content": "hi"}])
    assert resp.text == "ok"


def test_delta_payload_filters_structural_events() -> None:
    assert llm_mod._delta_payload(_StructuralEvent(type="message_start")) is None
    assert llm_mod._delta_payload(_StructuralEvent(type="content_block_start")) is None


def test_delta_payload_translates_text_delta() -> None:
    ev = _ContentBlockDeltaEvent(
        type="content_block_delta", index=2, delta=_TextDelta(type="text_delta", text="hi")
    )
    assert llm_mod._delta_payload(ev) == {"type": "text", "index": 2, "text": "hi"}


def test_delta_payload_translates_input_json_delta() -> None:
    ev = _ContentBlockDeltaEvent(
        type="content_block_delta", index=0, delta=_JsonDelta(type="input_json_delta", partial_json='{"k":')
    )
    assert llm_mod._delta_payload(ev) == {"type": "input_json", "index": 0, "partial_json": '{"k":'}
