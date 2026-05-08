from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from jazz_guru.db import session_scope
from jazz_guru.eval.judge import judge
from jazz_guru.harness import AgentLoop, SessionManager
from jazz_guru.logging import get_logger
from jazz_guru.state import EvalRun, list_session_artifacts

log = get_logger(__name__)


TASKS_DIR = Path(__file__).resolve().parent / "tasks"


@dataclass
class GoldenTask:
    id: str
    prompt: str
    rubric: dict[str, float]
    expected: str | None = None
    success_threshold: float = 0.6


def load_tasks(directory: Path | None = None) -> list[GoldenTask]:
    base = directory or TASKS_DIR
    out: list[GoldenTask] = []
    if not base.exists():
        return out
    for path in sorted(base.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        out.append(
            GoldenTask(
                id=str(data.get("id") or path.stem),
                prompt=str(data["prompt"]),
                rubric={k: float(v) for k, v in (data.get("rubric") or {}).items()},
                expected=data.get("expected"),
                success_threshold=float(data.get("success_threshold", 0.6)),
            )
        )
    return out


async def run_task(task: GoldenTask) -> dict[str, Any]:
    sm = SessionManager()
    handle = await sm.create(title=f"eval/{task.id}")
    loop = AgentLoop(handle)
    res = await loop.step(task.prompt)
    artifacts = list_session_artifacts(handle.id)
    judged = await judge(
        task=task.prompt,
        response=res.text,
        rubric=task.rubric or {"correctness": 1.0},
        expected=task.expected,
        artifacts=artifacts,
    )
    async with session_scope() as s:
        s.add(
            EvalRun(
                task_id=task.id,
                session_id=handle.id,
                score=judged.weighted_total,
                rubric=judged.raw,
                notes=judged.rationale[:1000],
            )
        )
    passed = judged.weighted_total >= task.success_threshold
    return {
        "task_id": task.id,
        "session_id": str(handle.id),
        "score": judged.weighted_total,
        "passed": passed,
        "rationale": judged.rationale,
        "artifacts": artifacts,
        "errors": res.errors,
    }


async def run_all(*, only: str | None = None) -> dict[str, Any]:
    tasks = load_tasks()
    if only:
        tasks = [t for t in tasks if t.id == only]
    if not tasks:
        return {"results": [], "pass_rate": 0.0, "count": 0}
    results = []
    for t in tasks:
        try:
            results.append(await run_task(t))
        except Exception as e:
            log.warning("eval.task_failed", task=t.id, err=str(e))
            results.append({"task_id": t.id, "passed": False, "error": str(e), "score": 0.0})
    passed = sum(1 for r in results if r.get("passed"))
    return {
        "count": len(results),
        "passed": passed,
        "pass_rate": passed / max(1, len(results)),
        "results": results,
    }


def regression_job(only: str | None = None) -> dict[str, Any]:
    """Sync entrypoint for the RQ worker."""
    return asyncio.run(run_all(only=only))
