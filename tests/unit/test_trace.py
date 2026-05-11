from __future__ import annotations

import json
import uuid
from pathlib import Path

from jazz_guru.logging.trace import TraceWriter


def test_trace_writer_appends(tmp_path: Path) -> None:
    sid = uuid.uuid4()
    tw = TraceWriter(sid, base_dir=tmp_path)
    tw.write("turn_start", {"input": "hi"})
    tw.write("turn_end", {"text": "ok"})
    lines = tw.path.read_text().strip().splitlines()
    assert len(lines) == 2
    rec0 = json.loads(lines[0])
    assert rec0["type"] == "turn_start"
    assert rec0["session_id"] == str(sid)
    assert rec0["payload"]["input"] == "hi"
