from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from jazz_guru.config import get_goal
from jazz_guru.db import session_scope
from jazz_guru.distillation.playbook import upsert_entry
from jazz_guru.llm import complete
from jazz_guru.logging import get_logger
from jazz_guru.memory import get_memory, summarize_history
from jazz_guru.state import (
    EventType,
    Turn,
    list_session_artifacts,
    load_latest,
    log_event,
    write_snapshot,
)

log = get_logger(__name__)


REFLEXION_SYSTEM = """You are the agent's offline reflexion loop.
You receive a session goal block, a transcript summary, the current self-model,
the artifacts produced, and any errors. Produce a strict JSON object with keys:

{
  "score": 0.0-1.0,            # how well this session served the goal
  "critique": "...",           # 3-6 sentences, concrete, actionable
  "revised_plan": "...",       # the plan the agent should follow next
  "open_threads": ["..."],     # short bullets
  "memory_writes": [           # durable observations to write to memory
    {"text": "...", "kind": "lesson"}
  ],
  "playbook_entries": [        # transferable heuristics
    {"scope": "voicing|rhythm|workflow|...", "text": "...", "score": 0.0-1.0}
  ]
}

Reply with ONLY the JSON object, no prose."""


@dataclass
class ReflectionResult:
    session_id: uuid.UUID
    score: float
    critique: str
    revised_plan: str
    open_threads: list[str] = field(default_factory=list)
    memory_writes: list[dict[str, Any]] = field(default_factory=list)
    playbook_entries: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


async def _gather_session_text(session_id: uuid.UUID) -> tuple[str, list[dict[str, Any]]]:
    async with session_scope() as s:
        turns = (
            await s.execute(
                select(Turn).where(Turn.session_id == session_id).order_by(Turn.idx.asc())
            )
        ).scalars().all()
    history: list[dict[str, Any]] = []
    for t in turns:
        if t.role in ("user", "assistant"):
            text = ""
            if isinstance(t.content, dict):
                text = t.content.get("text", "")
            history.append({"role": t.role, "content": text})
    summary = await summarize_history(history)
    return summary, history


async def run_reflexion(session_id: uuid.UUID) -> ReflectionResult:
    log.info("reflexion.start", session_id=str(session_id))
    goal = get_goal()
    summary, _history = await _gather_session_text(session_id)
    snap = load_latest(session_id) or {}
    artifacts = list_session_artifacts(session_id)

    prompt = f"""## Goal\n{goal.render_system_block()}

## Transcript summary
{summary or '(empty)'}

## Current self-model
{json.dumps(snap, indent=2)}

## Artifacts produced
{json.dumps(artifacts, indent=2)}
"""
    resp = await complete(
        [{"role": "user", "content": prompt}],
        system=REFLEXION_SYSTEM,
        max_tokens=2048,
        temperature=0.3,
    )
    try:
        data = _parse_json(resp.text)
    except Exception as e:
        log.warning("reflexion.parse_failed", err=str(e), text=resp.text[:300])
        data = {"score": 0.0, "critique": resp.text[:500], "revised_plan": ""}

    # Validate and normalize: a parseable-but-malformed model response can
    # break the strict coercions below (e.g. score="n/a" -> ValueError) or
    # silently corrupt state (e.g. open_threads="foo" -> ["f","o","o"]).
    def _to_float(v: Any, default: float) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def _to_str_list(v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(x) for x in v]
        return []

    def _to_dict_list(v: Any) -> list[dict[str, Any]]:
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
        return []

    result = ReflectionResult(
        session_id=session_id,
        score=max(0.0, min(1.0, _to_float(data.get("score"), 0.0))),
        critique=str(data.get("critique", "") or ""),
        revised_plan=str(data.get("revised_plan", "") or ""),
        open_threads=_to_str_list(data.get("open_threads")),
        memory_writes=_to_dict_list(data.get("memory_writes")),
        playbook_entries=_to_dict_list(data.get("playbook_entries")),
        raw=data,
    )

    mem = get_memory()
    for w in result.memory_writes:
        text = w.get("text")
        if not text:
            continue
        try:
            await mem.write(text=text, kind=w.get("kind", "lesson"), session_id=session_id, score=0.7)
        except Exception as e:
            log.warning("reflexion.memory_write_failed", err=str(e))

    for pe in result.playbook_entries:
        text = pe.get("text")
        scope = pe.get("scope", "general")
        if not text:
            continue
        # Coerce defensively — a model value like "n/a" would otherwise
        # raise here and silently drop an otherwise valid playbook entry.
        score = max(0.0, min(1.0, _to_float(pe.get("score"), 0.5)))
        try:
            await upsert_entry(scope, text, score=score)
        except Exception as e:
            log.warning("reflexion.playbook_upsert_failed", err=str(e))

    new_snap = {
        **snap,
        "summary": summary,
        "open_threads": result.open_threads,
        "artifacts": artifacts,
        "last_critique": result.critique,
        "last_plan": result.revised_plan,
        "last_score": result.score,
    }
    try:
        await write_snapshot(session_id, new_snap)
    except Exception as e:
        log.warning("reflexion.snapshot_failed", err=str(e))

    await log_event(
        session_id=session_id,
        event_type=EventType.REFLEXION.value,
        payload={
            "score": result.score,
            "critique": result.critique[:500],
            "memory_writes": len(result.memory_writes),
            "playbook_entries": len(result.playbook_entries),
        },
    )
    try:
        from jazz_guru.distillation.scheduler import enqueue_eval

        enqueue_eval()
    except Exception as e:  # redis may not be up in tests
        log.info("reflexion.eval_enqueue_skipped", err=str(e))
    log.info("reflexion.done", score=result.score)
    return result


def reflexion_job(session_id_str: str) -> dict[str, Any]:
    """Sync entrypoint for RQ worker."""
    import asyncio

    res = asyncio.run(run_reflexion(uuid.UUID(session_id_str)))
    return {
        "session_id": str(res.session_id),
        "score": res.score,
        "critique": res.critique[:300],
        "memory_writes": len(res.memory_writes),
        "playbook_entries": len(res.playbook_entries),
    }
