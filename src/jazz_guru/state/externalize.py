from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jazz_guru.config import get_settings
from jazz_guru.state.snapshot import snapshot_dir


@dataclass
class StateDoc:
    summary: str
    open_threads: list[str]
    artifacts: list[str]
    last_critique: str | None = None

    def render_markdown(self) -> str:
        out = ["### Summary", self.summary or "(none)"]
        if self.open_threads:
            out.append("\n### Open threads")
            out.extend(f"- {t}" for t in self.open_threads)
        if self.artifacts:
            out.append("\n### Artifacts so far")
            out.extend(f"- {a}" for a in self.artifacts)
        if self.last_critique:
            out.append("\n### Last reflexion critique\n" + self.last_critique)
        return "\n".join(out)


def load_latest(session_id: uuid.UUID) -> dict[str, Any] | None:
    p = snapshot_dir(session_id) / "latest.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def state_from_snapshot(payload: dict[str, Any] | None) -> StateDoc:
    if not payload:
        return StateDoc(summary="", open_threads=[], artifacts=[])
    return StateDoc(
        summary=payload.get("summary", ""),
        open_threads=list(payload.get("open_threads", [])),
        artifacts=list(payload.get("artifacts", [])),
        last_critique=payload.get("last_critique"),
    )


def list_session_artifacts(session_id: uuid.UUID) -> list[str]:
    s = get_settings()
    sess_dir: Path = s.jg_workspace_dir / "sessions" / str(session_id)
    if not sess_dir.exists():
        return []
    out: list[str] = []
    for p in sorted(sess_dir.rglob("*")):
        if p.is_file():
            out.append(str(p.relative_to(sess_dir)))
    return out
