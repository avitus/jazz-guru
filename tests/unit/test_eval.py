from __future__ import annotations

import uuid
from pathlib import Path

from jazz_guru.config import get_settings
from jazz_guru.eval import load_tasks, load_trace, summarize_trace
from jazz_guru.logging.trace import TraceWriter


def test_load_tasks_finds_yaml() -> None:
    tasks = load_tasks()
    ids = {t.id for t in tasks}
    assert "blues_lead_sheet" in ids
    assert "midi_arpeggio" in ids
    for t in tasks:
        assert t.prompt
        assert t.success_threshold > 0.0


def test_summarize_trace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "jg_trace_dir", tmp_path)
    sid = uuid.uuid4()
    tw = TraceWriter(sid, base_dir=tmp_path)
    tw.write("turn_start", {"input": "hi"})
    tw.write("tool_use", {"name": "fs_read"})
    tw.write("tool_result", {"name": "fs_read", "ok": True})
    tw.write("turn_end", {"text": "done"})
    recs = load_trace(sid)
    summary = summarize_trace(recs)
    assert summary.turns == 1
    assert summary.tool_calls == 1
    assert summary.final_text == "done"


def test_io_adapters() -> None:
    from jazz_guru.io import text, to_user_message

    msg = to_user_message(text("hello"))
    assert msg == "hello"
