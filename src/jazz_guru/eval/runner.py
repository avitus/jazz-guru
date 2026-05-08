from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jazz_guru.config import get_settings
from jazz_guru.logging import get_logger

log = get_logger(__name__)


@dataclass
class TraceRecord:
    ts: str
    type: str
    payload: dict[str, Any]


@dataclass
class TraceSummary:
    session_id: str
    turns: int
    tool_calls: int
    final_text: str
    errors: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)


def load_trace(session_id: str | uuid.UUID) -> list[TraceRecord]:
    p: Path = get_settings().jg_trace_dir / f"{session_id}.jsonl"
    if not p.exists():
        return []
    out: list[TraceRecord] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        out.append(TraceRecord(ts=rec["ts"], type=rec["type"], payload=rec.get("payload", {})))
    return out


def summarize_trace(records: list[TraceRecord]) -> TraceSummary:
    sid = ""
    turns = 0
    tool_calls = 0
    final_text = ""
    errors: list[str] = []
    for r in records:
        if r.type == "turn_start":
            turns += 1
        elif r.type == "turn_end":
            final_text = r.payload.get("text", final_text) or final_text
        elif r.type == "tool_use":
            tool_calls += 1
        elif r.type == "error":
            errors.append(r.payload.get("error", ""))
    return TraceSummary(
        session_id=sid, turns=turns, tool_calls=tool_calls, final_text=final_text, errors=errors
    )
