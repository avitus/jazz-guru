"""Blocks Network handler for the local jazz-guru agent harness.

Routes a `skill` discriminator in the request JSON to the in-process
jazz_guru APIs (chat / distill / evalrun / render_midi). The Blocks
runtime calls `handler(task, ctx)` synchronously; we drive jazz-guru's
async coroutines via `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

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


def _parse_request(task: StartTaskMessage) -> dict[str, Any]:
    parts = task.request_parts or []
    if not parts:
        raise ValueError("no request_parts")
    part = parts[0]
    raw = getattr(part, "text", None)
    if raw is None:
        raise ValueError("request_parts[0] has no text payload")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("{"):
            return json.loads(stripped)
        return {"skill": "chat", "message": raw}
    raise ValueError(f"request_parts[0].text has unexpected type {type(raw).__name__}")


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


async def _entrypoint(
    runner: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    payload: dict[str, Any],
) -> dict[str, Any]:
    # asyncpg connections are bound to the loop that opened them. Blocks
    # invokes us via asyncio.run per request, so the cached SQLAlchemy
    # engine from a prior request points at a dead loop. Dispose inside
    # this loop and let the lru_cache be cleared after asyncio.run exits.
    from jazz_guru.db import get_engine

    try:
        return await runner(payload)
    finally:
        if get_engine.cache_info().currsize:
            await get_engine().dispose()


def handler(task: StartTaskMessage, ctx: Optional[TaskContext] = None) -> dict[str, Any]:
    from jazz_guru.db import get_engine, get_sessionmaker

    payload = _parse_request(task)
    skill = payload.get("skill", "chat")
    runner = _DISPATCH.get(skill)
    if runner is None:
        raise ValueError(f"unknown skill {skill!r}; expected one of {sorted(_DISPATCH)}")
    if ctx is not None:
        ctx.report_status(f"jazz_guru: {skill}")
    try:
        result = asyncio.run(_entrypoint(runner, payload))
    finally:
        get_sessionmaker.cache_clear()
        get_engine.cache_clear()
    return {"artifacts": [{"data": json.dumps(result), "mimeType": "application/json"}]}
