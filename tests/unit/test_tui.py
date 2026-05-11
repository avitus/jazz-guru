"""Tests for the streaming-related additions to the Textual TUI.

We don't test the broader TUI surface area here (chat rendering, mic/PTT,
artifact polling); the focus is the new ``text_delta`` handling and the
streaming bubble lifecycle introduced for incremental responses.
"""
from __future__ import annotations

from typing import Any

import pytest
from textual.widgets import RichLog, Static

from jazz_guru.client.tui import JazzGuruTui


class _OfflineTui(JazzGuruTui):
    """Subclass that skips the network handshake in on_mount. The TUI's
    real on_mount opens a JazzGuruClient and pings the server — none of
    which we want during tests of pure event handling."""

    async def on_mount(self) -> None:  # type: ignore[override]
        return None


def _events_text(app: JazzGuruTui) -> str:
    log = app.query_one("#events", RichLog)
    return "\n".join(str(line) for line in log.lines)


def _streaming_text(app: JazzGuruTui) -> str:
    s = app.query_one("#streaming", Static)
    return str(s.render())


def _chat_text(app: JazzGuruTui) -> str:
    log = app.query_one("#chat", RichLog)
    return "\n".join(str(line) for line in log.lines)


async def test_text_delta_accumulates_in_streaming_bubble() -> None:
    app = _OfflineTui()
    async with app.run_test():
        for chunk in ("hel", "lo ", "world"):
            app._handle_event({"type": "text_delta", "payload": {"delta": chunk}})
        assert app._chat_buf == ["hel", "lo ", "world"]
        # The bubble shows the (italicised) joined text.
        assert "hello world" in _streaming_text(app)


async def test_final_resets_streaming_and_writes_to_chat() -> None:
    app = _OfflineTui()
    async with app.run_test():
        for chunk in ("part ", "one"):
            app._handle_event({"type": "text_delta", "payload": {"delta": chunk}})
        assert app._chat_buf  # populated
        app._handle_event(
            {
                "type": "final",
                "text": "part one",
                "tool_calls": 0,
                "usage": {"input_tokens": 1, "output_tokens": 2, "cost_usd": 0.0001},
            }
        )
        # Streaming bubble cleared, chat now contains the final assistant line.
        assert app._chat_buf == []
        assert _streaming_text(app) == ""
        assert "part one" in _chat_text(app)


async def test_error_event_resets_streaming_buffer() -> None:
    """A mid-stream error must clear the in-flight buffer; otherwise the
    next turn would prepend orphaned deltas."""
    app = _OfflineTui()
    async with app.run_test():
        app._handle_event({"type": "text_delta", "payload": {"delta": "hi"}})
        app._handle_event({"type": "error", "error": "boom"})
        assert app._chat_buf == []
        assert _streaming_text(app) == ""


async def test_send_resets_streaming_state() -> None:
    """A leftover _chat_buf from a previous turn must not survive into the
    next one. _send() resets the buffer up-front; even if the previous turn
    crashed before final/error handling, the next user message starts
    cleanly."""
    app = _OfflineTui()
    async with app.run_test():
        # Pretend a previous turn left state behind.
        app._chat_buf.extend(["leftover"])
        app._update_streaming("leftover")
        # _send short-circuits because there's no client/session, but the
        # reset_streaming() call happens before that check returns.
        await app._send("new message")
        assert app._chat_buf == []
        assert _streaming_text(app) == ""


async def test_tool_result_with_manifest_shows_size_in_events() -> None:
    """When pruning persists a payload, the events pane should surface the
    on-disk byte size so the user knows where the heavy data went."""
    app = _OfflineTui()
    async with app.run_test():
        app._handle_event(
            {
                "type": "tool_result",
                "payload": {
                    "id": "tu_1",
                    "name": "fs_read",
                    "ok": True,
                    "manifest": {
                        "path": "/tmp/x/tool_outputs/fs_read_0_abc.json",
                        "size_bytes": 50_000,
                        "tool": "fs_read",
                    },
                },
            }
        )
        evts = _events_text(app)
        assert "fs_read" in evts
        assert "50000" in evts


async def test_tool_result_without_manifest_unchanged() -> None:
    """A small tool result has no manifest and the events pane line should
    be the bare ok marker — no spurious size annotation."""
    app = _OfflineTui()
    async with app.run_test():
        app._handle_event(
            {"type": "tool_result", "payload": {"id": "tu_1", "name": "fs_list", "ok": True}}
        )
        evts = _events_text(app)
        assert "fs_list" in evts
        assert "B → disk" not in evts


async def test_empty_text_delta_is_ignored() -> None:
    """Empty deltas occasionally slip through from the SDK; they shouldn't
    pollute _chat_buf with empty strings (which would make len(_chat_buf)
    misleading for any future buffer-size logic)."""
    app = _OfflineTui()
    async with app.run_test():
        app._handle_event({"type": "text_delta", "payload": {"delta": ""}})
        app._handle_event({"type": "text_delta", "payload": {"delta": "real"}})
        assert app._chat_buf == ["real"]


async def test_unknown_event_type_does_not_crash() -> None:
    """Forward-compat: a future server might emit event types the client
    doesn't recognize. We render them in the events pane but don't raise."""
    app = _OfflineTui()
    async with app.run_test():
        app._handle_event({"type": "unknown_thing", "payload": {"x": 1}})
        # Bottomed out in the catch-all branch — should appear in events log.
        assert "unknown_thing" in _events_text(app)


async def test_streaming_bubble_truncates_to_tail() -> None:
    """For very long generations the bubble must clip to the trailing 800
    chars so the input row doesn't get pushed below the viewport."""
    app = _OfflineTui()
    async with app.run_test():
        head = "A" * 2_000
        tail = "Z" * 50
        app._handle_event({"type": "text_delta", "payload": {"delta": head + tail}})
        rendered = _streaming_text(app)
        # Tail must be present, head must have been dropped from the rendered view.
        assert "Z" * 50 in rendered
        assert "A" * 1_000 not in rendered


@pytest.fixture(autouse=True)
def _quiet_textual_warnings() -> Any:
    """Textual prints noisy DeprecationWarning lines via stderr in some
    configurations; suppress them here so the test output stays clean."""
    import warnings

    warnings.simplefilter("ignore", DeprecationWarning)
    yield
    warnings.resetwarnings()
