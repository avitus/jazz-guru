"""Mine tool failures from session trace JSONL.

The reflexion-driven improver (plan §B.1) consumes this to decide which
Tier-2 tools to propose patches for. Pure trace parsing — no DB, no LLM.

Failure detection is a union of three signals emitted by
``ActionController`` (see ``actions/controller.py``):

* ``ok: False`` — the handler raised; ``error`` carries the message.
* ``error`` present without ``ok`` — policy denial or budget exceeded.
* ``result_has_error: True`` — the handler returned an ``__error__``
  envelope (the dynamic-tool subprocess crashed or timed out). The
  paired ``error_excerpt`` carries the first 200 chars.
"""
from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jazz_guru.config import get_settings

__all__ = [
    "FailureRecord",
    "extract_from_session",
    "extract_tool_failures",
]


@dataclass
class FailureRecord:
    """One failed tool invocation, paired across tool_use + tool_result."""

    tool_name: str
    input: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    kind: str = ""  # "raised" | "policy" | "result_error"
    ts: str | None = None
    tool_use_id: str | None = None


def _load_records_from_path(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    # Stream line-by-line so a giant trace doesn't materialize fully in
    # memory, and harden against IO / encoding failures — an unreadable
    # trace file shouldn't prevent the improvement loop from running on
    # other tools.
    try:
        with p.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except (OSError, UnicodeDecodeError):
        return []
    return out


def extract_tool_failures(
    records: list[dict[str, Any]],
) -> dict[str, list[FailureRecord]]:
    """Group failure records by tool name, preserving in-trace order.

    Empty input → empty dict. Tools that only succeeded → not present.
    """
    # Index tool_use events by id for input lookup.
    uses_by_id: dict[str, dict[str, Any]] = {}
    for rec in records:
        if rec.get("type") != "tool_use":
            continue
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        rid = payload.get("id")
        if isinstance(rid, str):
            uses_by_id[rid] = {"payload": payload, "ts": rec.get("ts")}

    out: dict[str, list[FailureRecord]] = defaultdict(list)
    for rec in records:
        if rec.get("type") != "tool_result":
            continue
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        name = payload.get("name")
        if not isinstance(name, str):
            continue
        # Identify failures via the three signals documented above.
        ok = payload.get("ok")
        has_error_key = "error" in payload
        result_has_error = bool(payload.get("result_has_error"))
        if ok is True and not has_error_key and not result_has_error:
            continue
        kind = (
            "raised"
            if ok is False
            else "result_error"
            if result_has_error
            else "policy"
        )
        # Best-effort cross-reference back to the originating tool_use to
        # capture the input — the improver wants to replay it as a test.
        use_id = payload.get("id")
        use_rec = uses_by_id.get(use_id) if isinstance(use_id, str) else None
        input_args: dict[str, Any] = {}
        if use_rec is not None:
            up = use_rec["payload"]
            if isinstance(up.get("input"), dict):
                input_args = up["input"]
        error_message = payload.get("error") or payload.get("error_excerpt")
        out[name].append(
            FailureRecord(
                tool_name=name,
                input=input_args,
                error=str(error_message) if error_message else None,
                kind=kind,
                ts=use_rec["ts"] if use_rec else rec.get("ts"),
                tool_use_id=use_id if isinstance(use_id, str) else None,
            )
        )
    return dict(out)


def extract_from_session(session_id: str | uuid.UUID) -> dict[str, list[FailureRecord]]:
    """Convenience wrapper: load ``workspace/traces/<sid>.jsonl`` then parse."""
    try:
        sid = (
            session_id
            if isinstance(session_id, uuid.UUID)
            else uuid.UUID(str(session_id))
        )
    except (ValueError, TypeError):
        return {}
    trace_dir = Path(get_settings().jg_trace_dir).resolve()
    p = (trace_dir / f"{sid}.jsonl").resolve()
    try:
        p.relative_to(trace_dir)
    except ValueError:
        return {}
    return extract_tool_failures(_load_records_from_path(p))
