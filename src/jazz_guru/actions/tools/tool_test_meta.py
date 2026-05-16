"""Agent-facing meta-tools for managing Tier-2 tool test suites.

Four tools — ``tool_test_add``, ``tool_test_remove``, ``tool_test_list``,
``tool_test_run`` — let the agent author cases for a published tool,
inspect what's there, and run the suite. Test execution goes through the
same subprocess sandbox as the tool itself; results are persisted into
``generated_tool_test_runs`` for the improver to read later.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from jazz_guru.actions import store
from jazz_guru.actions.registry import registry
from jazz_guru.db import session_scope
from jazz_guru.state import (
    GeneratedTool,
    GeneratedToolTest,
    GeneratedToolTestRun,
)
from jazz_guru.testing.runner import TestCase, run_all

_CASE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def _validate_case_name(name: str) -> None:
    if not _CASE_NAME_RE.match(name):
        raise ValueError(
            f"invalid case name {name!r}; must match {_CASE_NAME_RE.pattern}"
        )


# ---------- tool_test_add -------------------------------------------------


class ToolTestAddInput(BaseModel):
    name: str = Field(..., description="Tier-2 tool name to attach the case to.")
    case_name: str = Field(
        ..., description="snake_case identifier for the case (unique per tool)."
    )
    case_spec: dict[str, Any] = Field(
        ...,
        description=(
            "Test case body. Must contain 'case': {input, predicate?} and may "
            "also contain 'rubric' and 'timeout_sec'. See plan §A.2 for shape."
        ),
    )
    enabled: bool = Field(True, description="Disabled cases are skipped by tool_test_run.")
    origin: str = Field(
        "agent_authored",
        description="Audit tag. Leave default unless you're the improver.",
    )


@registry.register(
    "tool_test_add",
    description=(
        "Attach a test case to a Tier-2 tool. Idempotent on (tool, case_name): "
        "re-adding overwrites the prior spec. Cases without a predicate AND "
        "without a rubric act as smoke tests (pass if the tool returns without "
        "error)."
    ),
    input_model=ToolTestAddInput,
    tags=("meta", "testing"),
)
async def tool_test_add(
    name: str,
    case_name: str,
    case_spec: dict[str, Any],
    enabled: bool = True,
    origin: str = "agent_authored",
) -> dict[str, Any]:
    try:
        _validate_case_name(case_name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not isinstance(case_spec, dict):
        return {"ok": False, "error": "case_spec must be an object"}

    async with session_scope() as s:
        tool = (
            await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
        ).scalar_one_or_none()
        if tool is None:
            return {"ok": False, "error": f"unknown tool {name!r}"}
        existing = (
            await s.execute(
                select(GeneratedToolTest)
                .where(GeneratedToolTest.tool_id == tool.id)
                .where(GeneratedToolTest.name == case_name)
            )
        ).scalar_one_or_none()
        if existing is None:
            row = GeneratedToolTest(
                tool_id=tool.id,
                name=case_name,
                spec=case_spec,
                origin=origin,
                enabled=enabled,
            )
            s.add(row)
            await s.flush()
            return {"ok": True, "id": str(row.id), "status": "created"}
        existing.spec = case_spec
        existing.enabled = enabled
        existing.origin = origin
        await s.flush()
        return {"ok": True, "id": str(existing.id), "status": "updated"}


# ---------- tool_test_remove ----------------------------------------------


class ToolTestRemoveInput(BaseModel):
    name: str
    case_name: str
    disable_only: bool = Field(
        False,
        description=(
            "If true, set enabled=False instead of deleting. Useful for muting "
            "a flaky case without losing its definition or run history."
        ),
    )


@registry.register(
    "tool_test_remove",
    description=(
        "Remove or disable a test case. With disable_only=True the row is "
        "kept (preserving generated_tool_test_runs FKs) but skipped by "
        "tool_test_run."
    ),
    input_model=ToolTestRemoveInput,
    tags=("meta", "testing"),
)
async def tool_test_remove(
    name: str, case_name: str, disable_only: bool = False
) -> dict[str, Any]:
    async with session_scope() as s:
        tool = (
            await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
        ).scalar_one_or_none()
        if tool is None:
            return {"ok": False, "error": f"unknown tool {name!r}"}
        existing = (
            await s.execute(
                select(GeneratedToolTest)
                .where(GeneratedToolTest.tool_id == tool.id)
                .where(GeneratedToolTest.name == case_name)
            )
        ).scalar_one_or_none()
        if existing is None:
            return {"ok": False, "error": f"no case {case_name!r} for {name!r}"}
        if disable_only:
            existing.enabled = False
            return {"ok": True, "status": "disabled"}
        await s.delete(existing)
        return {"ok": True, "status": "removed"}


# ---------- tool_test_list ------------------------------------------------


class ToolTestListInput(BaseModel):
    name: str
    include_disabled: bool = False


@registry.register(
    "tool_test_list",
    description=(
        "List the test cases attached to a Tier-2 tool. Includes the last "
        "run outcome per case when one exists."
    ),
    input_model=ToolTestListInput,
    tags=("meta", "testing"),
)
async def tool_test_list(name: str, include_disabled: bool = False) -> dict[str, Any]:
    async with session_scope() as s:
        tool = (
            await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
        ).scalar_one_or_none()
        if tool is None:
            return {"ok": False, "error": f"unknown tool {name!r}"}
        q = select(GeneratedToolTest).where(GeneratedToolTest.tool_id == tool.id)
        if not include_disabled:
            q = q.where(GeneratedToolTest.enabled.is_(True))
        cases = (
            await s.execute(q.order_by(GeneratedToolTest.name.asc()))
        ).scalars().all()
        items: list[dict[str, Any]] = []
        for c in cases:
            # Latest run per case — cheap with the (tool_id, ran_at) index.
            last = (
                await s.execute(
                    select(GeneratedToolTestRun)
                    .where(GeneratedToolTestRun.test_id == c.id)
                    .order_by(GeneratedToolTestRun.ran_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            items.append(
                {
                    "name": c.name,
                    "enabled": c.enabled,
                    "origin": c.origin,
                    "last_run": (
                        {
                            "passed": last.passed,
                            "ms": last.ms,
                            "judge_score": last.judge_score,
                            "ran_at": last.ran_at.isoformat() if last.ran_at else None,
                        }
                        if last
                        else None
                    ),
                }
            )
        return {"ok": True, "name": name, "cases": items}


# ---------- tool_test_run -------------------------------------------------


class ToolTestRunInput(BaseModel):
    name: str
    case_name: str | None = Field(
        None, description="Run only this case. Default: run every enabled case."
    )
    use_judge: bool = Field(
        False,
        description=(
            "Pass a judge_task_label so rubric cases evaluate. Costs an LLM "
            "call per rubric case; default false to keep ad-hoc runs cheap."
        ),
    )


@registry.register(
    "tool_test_run",
    description=(
        "Run a tool's test suite against the current published source. "
        "Persists each case's outcome into generated_tool_test_runs and "
        "returns an aggregate summary plus per-case details."
    ),
    input_model=ToolTestRunInput,
    tags=("meta", "testing"),
)
async def tool_test_run(
    name: str, case_name: str | None = None, use_judge: bool = False
) -> dict[str, Any]:
    spec = await store.get_spec(name)
    if spec is None:
        return {"ok": False, "error": f"unknown tool {name!r}"}
    cases = await store.list_tests(name)
    if case_name is not None:
        cases = [c for c in cases if c.name == case_name]
        if not cases:
            return {"ok": False, "error": f"no enabled case {case_name!r} for {name!r}"}
    if not cases:
        return {
            "ok": True,
            "name": name,
            "total": 0,
            "passed": 0,
            "failed": 0,
            "cases": [],
            "note": "no enabled cases for this tool",
        }

    test_cases = [TestCase.from_spec(c.name, c.spec or {}) for c in cases]
    label = f"test run for {name}" if use_judge else None
    results = await run_all(spec, test_cases, judge_task_label=label)

    # Persist run outcomes for the improver and CLI to read. `cases` are
    # detached after the list_tests() call returned, but expire_on_commit=False
    # on the sessionmaker keeps their attributes readable.
    by_name = {c.name: c for c in cases}
    async with session_scope() as s:
        for r in results:
            test_row = by_name[r.case_name]
            s.add(
                GeneratedToolTestRun(
                    tool_id=test_row.tool_id,
                    tool_version=spec.version,
                    test_id=test_row.id,
                    passed=r.passed,
                    # ``output`` is a JSON column — any JSON-serializable
                    # shape is fine. Don't drop non-dict outputs (lists,
                    # scalars, None), since the audit log is more useful
                    # if it captures whatever the tool actually returned.
                    output=r.output,
                    error=r.error,
                    ms=r.ms,
                    judge_score=r.judge_score,
                    ran_at=datetime.now(UTC),
                )
            )

    passed = sum(1 for r in results if r.passed)
    return {
        "ok": True,
        "name": name,
        "version": spec.version,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "cases": [
            {
                "name": r.case_name,
                "passed": r.passed,
                "ms": r.ms,
                "error": r.error,
                "judge_score": r.judge_score,
                "failures": r.failures,
            }
            for r in results
        ],
    }
