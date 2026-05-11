from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from jazz_guru.config import get_settings

_configured = False
_lock = threading.Lock()


def _configure() -> None:
    global _configured
    with _lock:
        if _configured:
            return
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.dev.ConsoleRenderer(),
            ],
            cache_logger_on_first_use=True,
        )
        _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    _configure()
    return structlog.get_logger(name)


def _default(o: Any) -> Any:
    if isinstance(o, uuid.UUID):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    if hasattr(o, "model_dump"):
        return o.model_dump()
    return repr(o)


class TraceWriter:
    """Append-only JSONL writer for per-session traces."""

    def __init__(self, session_id: uuid.UUID, base_dir: Path | None = None) -> None:
        base = base_dir or get_settings().jg_trace_dir
        base.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        self.path = base / f"{session_id}.jsonl"
        self._lock = threading.Lock()

    def write(self, event_type: str, payload: dict[str, Any]) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "session_id": str(self.session_id),
            "type": event_type,
            "payload": payload,
        }
        line = json.dumps(record, default=_default, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
