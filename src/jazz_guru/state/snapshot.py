from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jazz_guru.config import get_settings
from jazz_guru.db import session_scope
from jazz_guru.state.schema import Snapshot


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


def snapshot_dir(session_id: uuid.UUID) -> Path:
    base = get_settings().jg_state_dir / str(session_id)
    base.mkdir(parents=True, exist_ok=True)
    return base


async def write_snapshot(
    session_id: uuid.UUID,
    payload: dict[str, Any],
    *,
    turn_id: uuid.UUID | None = None,
    turn_idx: int | None = None,
) -> Path:
    base = snapshot_dir(session_id)
    if turn_idx is not None:
        label = f"turn_{turn_idx:05d}"
    else:
        # Microsecond precision + uuid suffix prevents within-second collisions
        # if write_snapshot is called multiple times in tight succession.
        label = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ") + f"_{uuid.uuid4().hex[:8]}"
    path = base / f"{label}.json"
    raw = json.dumps(payload, indent=2, default=_default).encode("utf-8")
    # File I/O off the event loop.
    await asyncio.to_thread(path.write_bytes, raw)
    sha = hashlib.sha256(raw).hexdigest()
    async with session_scope() as s:
        s.add(Snapshot(session_id=session_id, turn_id=turn_id, path=str(path), sha256=sha))
    latest = base / "latest.json"
    await asyncio.to_thread(latest.write_bytes, raw)
    return path
