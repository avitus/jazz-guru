"""Blocks Network handler for the local jazz-guru agent harness.

Routes a `skill` discriminator (in a text part) to the in-process
jazz_guru APIs (chat / distill / evalrun / render_midi). The Blocks
runtime calls `handler(task, ctx)` synchronously; we drive jazz-guru's
async coroutines via `asyncio.run`.

Binary `request_parts` (audio WAV / MIDI / MusicXML) are downloaded via
`ctx.download_input_artifact` and dropped into `<workspace>/sessions/<sid>/in/`
so the agent's existing tools (`fs_read`, `audio_analyze`, `music_xml_*`)
can pick them up via the same `in/<filename>` paths the FastAPI
`POST /uploads` endpoint produces. Outputs are computed as the
**delta** of `list_session_artifacts(sid)` before vs after the
dispatch — new/modified files this turn are returned as separate
Blocks artifacts alongside the existing JSON envelope.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import sys
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from blocks_network import StartTaskMessage, TaskContext

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _bootstrap_jazz_guru() -> None:
    """Make `import jazz_guru` work and surface its `.env` to settings.

    `blocks run` launches us from `blocks/jazz_guru/`, two levels below
    the repo. Two side effects matter:
      1. Add `<repo>/src` to sys.path so the editable jazz_guru package
         resolves even if the venv doesn't have it installed.
      2. Load `<repo>/.env` into `os.environ` so
         `jazz_guru.config.Settings` (CWD-relative env_file) still sees
         ANTHROPIC_API_KEY / DATABASE_URL / etc.
    """
    src = _REPO_ROOT / "src"
    src_str = str(src)
    if src.is_dir() and src_str not in sys.path:
        sys.path.insert(0, src_str)

    env_path = _REPO_ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_bootstrap_jazz_guru()


def _register_extra_mimetypes() -> None:
    """Patch entries Python's stdlib mimetypes DB misses or gets wrong.

    The Blocks marketplace surfaces these MIME types verbatim, and
    DAWs / notation editors care about the audio/midi vs application/midi
    distinction. Idempotent — safe to call repeatedly.
    """
    mimetypes.add_type("audio/midi", ".mid")
    mimetypes.add_type("audio/midi", ".midi")
    mimetypes.add_type("audio/wav", ".wav")
    mimetypes.add_type("audio/mpeg", ".mp3")
    mimetypes.add_type("audio/flac", ".flac")
    mimetypes.add_type("application/vnd.recordare.musicxml+xml", ".musicxml")
    mimetypes.add_type("application/vnd.recordare.musicxml", ".mxl")
    mimetypes.add_type("application/xml", ".xml")


_register_extra_mimetypes()


# ---------------------------------------------------------------------------
# Input decoding
# ---------------------------------------------------------------------------


def _safe_upload_name(raw: str | None, fallback: str = "upload.bin") -> str:
    """Sanitize an incoming filename into a basename-only string.

    Mirrors the FastAPI POST /uploads sanitizer at server.py:372 so
    operators see consistent paths regardless of entry point.
    """
    candidate = Path(raw or fallback).name.lstrip(".")
    return candidate or fallback


def _decode_artifact_bytes(part: Any, ctx: TaskContext | None) -> bytes:
    """Return the raw bytes for a `request_parts[i]` with an artifact_ref.

    Prefers `ctx.download_input_artifact(part)` (handles both inline and
    PubNub-file variants via PAM token). Falls back to direct base64
    decode for inline parts when ctx is absent (CLI / unit tests).
    """
    if ctx is not None:
        try:
            return ctx.download_input_artifact(part)
        except Exception:
            # Fall through to inline decode below — useful when running
            # under a stripped ctx that lacks the download helper.
            pass
    ref = getattr(part, "artifact_ref", None)
    if ref is None:
        raise ValueError("artifact part has no artifact_ref")
    if ref.kind != "inline" or not ref.data:
        raise ValueError(
            f"cannot decode {ref.kind!r} artifact without TaskContext.download_input_artifact"
        )
    return base64.b64decode(ref.data)


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _parse_text_parts(
    task: StartTaskMessage,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Pull the canonical skill payload + loose text from text parts.

    Used in both phases of dispatch: first to learn the session id
    before writing attachments, and again (via `_decode_inputs`) to
    finalize the payload after attachments are on disk.
    """
    payload: dict[str, Any] | None = None
    fragments: list[str] = []
    for part in task.request_parts or []:
        raw = getattr(part, "text", None)
        if raw is None:
            continue
        if isinstance(raw, dict):
            if payload is None and "skill" in raw:
                payload = dict(raw)
            else:
                fragments.append(json.dumps(raw))
            continue
        if not isinstance(raw, str):
            continue
        stripped = raw.strip()
        if payload is None and stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                fragments.append(stripped)
                continue
            if isinstance(parsed, dict) and "skill" in parsed:
                payload = parsed
            else:
                fragments.append(stripped)
            continue
        fragments.append(stripped)
    return payload, fragments


def _persist_attachments(
    task: StartTaskMessage,
    ctx: TaskContext | None,
    in_dir: Path,
) -> list[dict[str, Any]]:
    """Decode every artifact part, write to ``in_dir``, return manifests.

    Each entry: ``{"path": "in/<name>", "name": <name>, "mime": <mime>,
    "size": <bytes>}``.
    """
    parts = task.request_parts or []
    if not any(getattr(p, "artifact_ref", None) is not None for p in parts):
        return []
    in_dir.mkdir(parents=True, exist_ok=True)
    in_dir_resolved = in_dir.resolve()
    out: list[dict[str, Any]] = []
    for part in parts:
        artifact_ref = getattr(part, "artifact_ref", None)
        if artifact_ref is None:
            continue
        data = _decode_artifact_bytes(part, ctx)
        safe = _safe_upload_name(getattr(artifact_ref, "file_name", None))
        target = (in_dir / safe).resolve()
        try:
            target.relative_to(in_dir_resolved)
        except ValueError as e:
            raise ValueError(f"attachment filename escapes in/: {safe!r}") from e
        target.write_bytes(data)
        out.append(
            {
                "path": f"in/{safe}",
                "name": safe,
                "mime": getattr(artifact_ref, "mime_type", None)
                or mimetypes.guess_type(safe)[0]
                or "application/octet-stream",
                "size": len(data),
            }
        )
    return out


def _augment_payload_with_attachments(
    payload: dict[str, Any],
    attachments: list[dict[str, Any]],
) -> None:
    """Mutate ``payload`` so the dispatched skill sees the attached files."""
    if not attachments:
        return

    skill = payload.get("skill", "chat")
    if skill == "render_midi":
        midi_attachments = [a for a in attachments if a["name"].lower().endswith((".mid", ".midi"))]
        if not payload.get("midi_path") and len(midi_attachments) == 1:
            payload["midi_path"] = midi_attachments[0]["path"]
        elif not payload.get("midi_path") and len(midi_attachments) > 1:
            raise ValueError(
                "render_midi received multiple MIDI attachments without an explicit midi_path"
            )
        return

    # chat / score: append a structured hint to the message so the agent
    # knows what's available without needing a custom tool.
    lines = [
        f"- {a['path']}  ({a['mime']}, {_human_size(a['size'])})"
        for a in attachments
    ]
    hint = "Attached files (in the session workspace):\n" + "\n".join(lines)
    existing = (payload.get("message") or payload.get("text") or "").strip()
    payload["message"] = (existing + "\n\n" + hint) if existing else hint


# ---------------------------------------------------------------------------
# Skill dispatchers (unchanged contracts; envelope shape is the same).
# ---------------------------------------------------------------------------


async def _run_chat(payload: dict[str, Any]) -> dict[str, Any]:
    from jazz_guru.harness import AgentLoop, SessionManager

    message = payload.get("message") or payload.get("text")
    if not message:
        raise ValueError("chat requires a 'message' field")
    sm = SessionManager()
    if payload.get("session_id"):
        handle = await sm.load(uuid.UUID(payload["session_id"]))
    else:
        handle = await sm.create(title=payload.get("title"))
    loop = AgentLoop(handle)
    result = await loop.step(message)
    return {
        "skill": payload.get("skill", "chat"),
        "session_id": str(handle.id),
        "text": result.text,
        "tool_calls": result.tool_calls,
        "usage": {
            "input": result.usage.input_tokens,
            "output": result.usage.output_tokens,
            "usd": round(result.usage.cost_usd, 4),
        },
    }


async def _run_distill(payload: dict[str, Any]) -> dict[str, Any]:
    from jazz_guru.distillation import run_reflexion

    sid = payload.get("session_id")
    if not sid:
        raise ValueError("distill requires 'session_id'")
    r = await run_reflexion(uuid.UUID(sid))
    return {
        "skill": "distill",
        "session_id": sid,
        "score": r.score,
        "critique": r.critique,
        "playbook_entries": len(r.playbook_entries),
    }


async def _run_evalrun(payload: dict[str, Any]) -> dict[str, Any]:
    from jazz_guru.eval import run_all

    res = await run_all(only=payload.get("only"))
    return {"skill": "evalrun", "result": res}


async def _run_render_midi(payload: dict[str, Any]) -> dict[str, Any]:
    from jazz_guru.actions.context import ToolContext, reset_tool_context, set_tool_context
    from jazz_guru.actions.tools.render import RenderMidiInput, render_midi
    from jazz_guru.harness import SessionManager

    sid_str: str
    if payload.get("session_id"):
        sid_str = payload["session_id"]
        uuid.UUID(sid_str)  # validate
    else:
        handle = await SessionManager().create(title="render_midi")
        sid_str = str(handle.id)

    args = {k: v for k, v in payload.items() if k not in {"skill", "session_id"}}
    spec = RenderMidiInput(**args)
    token = set_tool_context(ToolContext(session_id=sid_str, turn_idx=0))
    try:
        out = await render_midi(**spec.model_dump(exclude_none=True))
    finally:
        reset_tool_context(token)
    return {"skill": "render_midi", "session_id": sid_str, "result": out}


_DISPATCH: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "chat": _run_chat,
    # `score` is a presentational variant of `chat` exposed in the agent card
    # (notation-focused prompts). Same handler; distinct id keeps the marketplace
    # contract honest with the card's `skills[]` listing.
    "score": _run_chat,
    "distill": _run_distill,
    "evalrun": _run_evalrun,
    "render_midi": _run_render_midi,
}


# ---------------------------------------------------------------------------
# Session-workspace helpers (artifact delta + path resolution).
# ---------------------------------------------------------------------------


async def _resolve_session_id(payload: dict[str, Any]) -> uuid.UUID:
    """Pick the session id we'll snapshot for the artifact delta.

    For skills that accept an existing session, the caller's value wins
    (no DB hit). Otherwise we create a fresh session row up front so the
    attachments we're about to write land in the same workspace the
    agent step (chat / score) or renderer will use, and so `_run_chat`
    can `sm.load(sid)` rather than re-creating.
    """
    sid_raw = payload.get("session_id")
    if sid_raw:
        return uuid.UUID(sid_raw)
    from jazz_guru.harness import SessionManager

    handle = await SessionManager().create(title=payload.get("title"))
    payload["session_id"] = str(handle.id)
    return handle.id


def _session_dir(sid: uuid.UUID) -> Path:
    from jazz_guru.config import get_settings

    return (get_settings().jg_workspace_dir / "sessions" / str(sid)).resolve()


def _snapshot_artifacts(sid: uuid.UUID) -> dict[str, float]:
    """Map of session-relative path → mtime for every file currently
    under the session workspace. We use mtime + path so we catch
    in-place modifications, not just newly-created files.
    """
    base = _session_dir(sid)
    if not base.exists():
        return {}
    out: dict[str, float] = {}
    for p in base.rglob("*"):
        if p.is_file():
            rel = str(p.relative_to(base))
            try:
                out[rel] = p.stat().st_mtime
            except OSError:
                continue
    return out


def _collect_new_artifacts(
    sid: uuid.UUID,
    before: dict[str, float],
) -> list[dict[str, Any]]:
    """Return Blocks-shaped artifact entries for files written or modified
    since the ``before`` snapshot, sorted by path for determinism.

    Skips snapshot bookkeeping inside ``state/`` so internal scratch
    files don't leak to the caller, and never emits an attachment we
    just wrote into ``in/`` (callers already have that bytes).
    """
    base = _session_dir(sid)
    if not base.exists():
        return []
    entries: list[dict[str, Any]] = []
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(base))
        if rel.startswith("in/"):
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        prior = before.get(rel)
        if prior is not None and abs(mtime - prior) < 1e-6:
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        entries.append(
            {
                "data": data,
                "mimeType": mime,
                "fileName": p.name,
                "_path": rel,  # internal: surfaced in the JSON envelope, stripped on return
            }
        )
    return entries


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _entrypoint(
    task: StartTaskMessage,
    ctx: TaskContext | None,
) -> dict[str, Any]:
    """Dispatch the skill, then build the artifact-rich response.

    asyncpg connections are bound to the loop that opened them. Blocks
    invokes us via asyncio.run per request, so the cached SQLAlchemy
    engine from a prior request points at a dead loop. Dispose inside
    this loop and let the lru_cache be cleared after asyncio.run exits.
    """
    from jazz_guru.db import get_engine

    # Resolve the session id first (so attachments land in the same
    # workspace the agent step will use), then write attachments to
    # `in/`, then finalize the payload.
    payload, fragments = _parse_text_parts(task)
    if payload is None:
        payload = {}
    sid = await _resolve_session_id(payload)
    in_dir = _session_dir(sid) / "in"

    attachments = _persist_attachments(task, ctx, in_dir)

    if not payload.get("skill"):
        joined = "\n\n".join(t for t in fragments if t)
        if not joined and not attachments:
            raise ValueError("request has no decodable parts")
        payload = {"skill": "chat", "session_id": str(sid), "message": joined or ""}
    elif fragments and not payload.get("message") and not payload.get("text"):
        payload["message"] = "\n\n".join(fragments)
    payload.setdefault("session_id", str(sid))

    _augment_payload_with_attachments(payload, attachments)

    skill = payload.get("skill", "chat")
    runner = _DISPATCH.get(skill)
    if runner is None:
        raise ValueError(f"unknown skill {skill!r}; expected one of {sorted(_DISPATCH)}")

    before = _snapshot_artifacts(sid)
    try:
        result = await runner(payload)
    finally:
        if get_engine.cache_info().currsize:
            await get_engine().dispose()

    new_artifacts = _collect_new_artifacts(sid, before)

    # Surface the artifact paths in the JSON envelope so machine
    # consumers reading only the first artifact still see the list.
    if isinstance(result, dict):
        result = dict(result)
        result["artifacts"] = [a["_path"] for a in new_artifacts]
        result["attachments"] = [a["path"] for a in attachments]

    envelope = [{"data": json.dumps(result), "mimeType": "application/json"}]
    for entry in new_artifacts:
        envelope.append(
            {
                "data": entry["data"],
                "mimeType": entry["mimeType"],
                "fileName": entry["fileName"],
            }
        )
    return {"artifacts": envelope}


def handler(task: StartTaskMessage, ctx: TaskContext | None = None) -> dict[str, Any]:
    from jazz_guru.db import get_engine, get_sessionmaker

    if ctx is not None:
        ctx.report_status("jazz_guru: dispatching")
    try:
        return asyncio.run(_entrypoint(task, ctx))
    finally:
        get_sessionmaker.cache_clear()
        get_engine.cache_clear()
