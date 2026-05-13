"""Reflexion-driven repair loop for Tier-2 dynamic tools (plan §B.3-B.5).

Given a tool that has accumulated failures, propose a new source via an
LLM call, run every existing test case + new cases derived from the
failures, and — only if everything passes — commit a new version. Old
source is preserved via ``store.upsert``'s automatic version snapshot.

This module is invoked from the reflexion loop (PR 7); it does not
trigger itself. It also does not touch the agent's runtime registry —
new versions are picked up on the next session's hydration pass.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from jazz_guru.actions import store
from jazz_guru.actions.dynamic import DynamicSpec, hash_source, validate_source
from jazz_guru.db import session_scope
from jazz_guru.llm import complete
from jazz_guru.logging import get_logger
from jazz_guru.state import GeneratedTool, GeneratedToolTest
from jazz_guru.testing.failure_signals import FailureRecord
from jazz_guru.testing.runner import TestCase, run_all

log = get_logger(__name__)


# Conservative defaults — overridable per-tool via ``meta``.
DEFAULT_THRESHOLD = 2  # failures-in-a-session required to attempt
MAX_ATTEMPTS = 3  # consecutive_failures before improve_locked


class ImproveStatus:
    """String constants — used as ``ImproveOutcome.status`` values."""

    SKIPPED = "skipped"
    SKIPPED_NO_TESTS = "skipped_no_tests"
    SKIPPED_LOCKED = "skipped_locked"
    LOCKED_NOW = "locked_now"
    PROPOSE_FAILED = "propose_failed"
    TESTS_FAILED = "tests_failed"
    NO_OP = "no_op"
    PASSED = "passed"


@dataclass
class ImproveOutcome:
    status: str
    tool_name: str
    new_version: int | None = None
    rationale: str | None = None
    failures: list[str] = field(default_factory=list)
    n_existing_pass: int = 0
    n_existing_fail: int = 0
    n_new_cases: int = 0


# ---------- proposal LLM call ---------------------------------------------


_PROPOSAL_SYSTEM = """You are the tool-repair component of an agent harness.

You receive the CURRENT source of a Tier-2 dynamic tool, the test cases
it must continue to pass, and recent failure records. Produce a strict
JSON object with these keys, and NOTHING else (no prose, no fences):

{
  "source": "...",            // new Python source defining def run(**kwargs)
  "rationale": "...",         // 2-4 sentences, concrete
  "new_test_cases": [         // cases derived from failures that the new source must handle
    {"name": "snake_case_name", "case": {"input": {...}, "predicate": {...}}}
  ],
  "schema_unchanged": true    // MUST be true; we reject changes
}

Hard constraints:
- The new source must accept the SAME ``input_schema`` as the current.
  Do not rename kwargs; do not add required ones.
- Do not introduce non-stdlib imports unless they were already used.
- Do not call out to the network or filesystem outside the session
  workspace (the tool runs in a sandboxed subprocess).
- Keep ``run(**kwargs)`` as the entrypoint (sync or async).
"""


def _build_proposal_prompt(
    spec: DynamicSpec,
    tests: list[GeneratedToolTest],
    failures: list[FailureRecord],
) -> str:
    test_block = "\n".join(
        f"- {t.name}: {json.dumps(t.spec or {}, ensure_ascii=False)[:600]}"
        for t in tests
    ) or "(no tests yet)"
    fail_block = "\n".join(
        f"- input={json.dumps(f.input)[:200]} → {f.kind}: {f.error}"
        for f in failures[:10]  # cap at 10 to avoid prompt bloat
    )
    return (
        f"## Tool: {spec.name} (version {spec.version})\n"
        f"### Description\n{spec.description}\n\n"
        f"### Input schema\n```json\n{json.dumps(spec.input_schema, indent=2)}\n```\n\n"
        f"### Current source\n```python\n{spec.source}\n```\n\n"
        f"### Existing test cases (must continue to pass)\n{test_block}\n\n"
        f"### Recent failures\n{fail_block}\n"
    )


def _parse_proposal(text: str) -> dict[str, Any] | None:
    """Defensive JSON extraction — same pattern as reflexion/_parse_json."""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        obj = json.loads(s[a : b + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


async def _propose_patch(
    spec: DynamicSpec,
    tests: list[GeneratedToolTest],
    failures: list[FailureRecord],
) -> dict[str, Any] | None:
    """One LLM call returning ``{source, rationale, new_test_cases, schema_unchanged}``."""
    prompt = _build_proposal_prompt(spec, tests, failures)
    try:
        resp = await complete(
            [{"role": "user", "content": prompt}],
            system=_PROPOSAL_SYSTEM,
            max_tokens=4096,
            temperature=0.2,
        )
    except Exception as e:
        log.warning("improver.proposal_call_failed", tool=spec.name, err=str(e))
        return None
    proposal = _parse_proposal(resp.text)
    if proposal is None:
        log.warning("improver.proposal_parse_failed", tool=spec.name, text=resp.text[:200])
        return None
    # Minimal structural checks; the rest is enforced by the gate.
    if not isinstance(proposal.get("source"), str):
        return None
    if proposal.get("schema_unchanged") is not True:
        log.info("improver.proposal_rejected_schema_change", tool=spec.name)
        return None
    return proposal


# ---------- lock + counter helpers ----------------------------------------


async def _bump_consecutive_failures(name: str) -> int:
    """Increment the per-tool consecutive_failures counter; return new value."""
    async with session_scope() as s:
        tool = (
            await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
        ).scalar_one_or_none()
        if tool is None:
            return 0
        meta = dict(tool.meta or {})
        meta["consecutive_failures"] = int(meta.get("consecutive_failures", 0) or 0) + 1
        tool.meta = meta
        return int(meta["consecutive_failures"])


async def _set_locked(name: str, reason: str) -> None:
    async with session_scope() as s:
        tool = (
            await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
        ).scalar_one_or_none()
        if tool is None:
            return
        meta = dict(tool.meta or {})
        meta["improve_locked"] = True
        meta["improve_lock_reason"] = reason
        tool.meta = meta


async def _reset_failures(name: str) -> None:
    async with session_scope() as s:
        tool = (
            await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
        ).scalar_one_or_none()
        if tool is None:
            return
        meta = dict(tool.meta or {})
        meta["consecutive_failures"] = 0
        tool.meta = meta


# ---------- commit ---------------------------------------------------------


def _derive_test_cases(
    failures: list[FailureRecord], proposal: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    """Build (case_name, spec) tuples from the proposal's new_test_cases.

    The proposal author (the LLM) is responsible for designing these — we
    don't synthesize them from raw failures alone, because the right
    predicate depends on what the fixed tool is supposed to return.
    """
    raw = proposal.get("new_test_cases") or []
    out: list[tuple[str, dict[str, Any]]] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        case_name = entry.get("name")
        case_body = entry.get("case")
        if not isinstance(case_name, str) or not isinstance(case_body, dict):
            continue
        out.append((case_name, {"case": case_body}))
    # The failures parameter is unused here but kept for future heuristic
    # synthesis (e.g. "every recent failing input must now return without
    # __error__"). Documenting the intent via the signature.
    _ = failures
    return out


async def _commit_new_version(
    *,
    name: str,
    proposal: dict[str, Any],
    spec: DynamicSpec,
    new_cases: list[tuple[str, dict[str, Any]]],
) -> int:
    """Apply the new source + new tests + reset the failure counter.

    Returns the new version number. ``store.upsert`` snapshots the prior
    state automatically; we tag origin="improver" with the proposal's
    rationale for audit.
    """
    new_source = str(proposal["source"])
    rationale = str(proposal.get("rationale", "")) or None
    # Preserve runtime status flags except for the failure counter, which
    # the success path resets to zero.
    new_meta = dict(spec.meta or {})
    new_meta["consecutive_failures"] = 0
    await store.upsert(
        name=name,
        description=spec.description,
        input_schema=spec.input_schema,
        source=new_source,
        scope=spec.scope,
        owner_session_id=spec.owner_session_id,
        meta=new_meta,
        origin="improver",
        rationale=rationale,
    )
    # The latest spec lives in the DB now; reload for the version number.
    refreshed = await store.get_spec(name)
    new_version = refreshed.version if refreshed else (spec.version + 1)
    # Attach the proposal's new test cases (best-effort; missing rows here
    # don't undo a successful publish).
    if new_cases:
        async with session_scope() as s:
            tool = (
                await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
            ).scalar_one()
            for case_name, case_spec in new_cases:
                existing = (
                    await s.execute(
                        select(GeneratedToolTest)
                        .where(GeneratedToolTest.tool_id == tool.id)
                        .where(GeneratedToolTest.name == case_name)
                    )
                ).scalar_one_or_none()
                if existing is None:
                    s.add(
                        GeneratedToolTest(
                            tool_id=tool.id,
                            name=case_name,
                            spec=case_spec,
                            origin="improver_added",
                            enabled=True,
                        )
                    )
                else:
                    existing.spec = case_spec
                    existing.origin = "improver_added"
                    existing.enabled = True
    return new_version


# ---------- main orchestrator ---------------------------------------------


async def maybe_improve(
    name: str, failures: list[FailureRecord]
) -> ImproveOutcome:
    """Propose a patch for ``name`` if it has tests, isn't locked, and isn't
    in its retry budget. The success/failure outcome drives lock state.
    """
    spec = await store.get_spec(name)
    if spec is None:
        return ImproveOutcome(status=ImproveStatus.SKIPPED, tool_name=name)
    spec_meta = spec.meta or {}
    if spec_meta.get("improve_locked"):
        return ImproveOutcome(status=ImproveStatus.SKIPPED_LOCKED, tool_name=name)
    if int(spec_meta.get("consecutive_failures", 0) or 0) >= MAX_ATTEMPTS:
        await _set_locked(name, reason="max_attempts")
        return ImproveOutcome(status=ImproveStatus.LOCKED_NOW, tool_name=name)

    tests = await store.list_tests(name)
    if not tests:
        return ImproveOutcome(status=ImproveStatus.SKIPPED_NO_TESTS, tool_name=name)

    proposal = await _propose_patch(spec, tests, failures)
    if proposal is None:
        await _bump_consecutive_failures(name)
        return ImproveOutcome(status=ImproveStatus.PROPOSE_FAILED, tool_name=name)

    # Validate source structurally before paying for a full test run.
    try:
        validate_source(proposal["source"])
    except Exception as e:
        log.info("improver.proposal_invalid_source", tool=name, err=str(e))
        await _bump_consecutive_failures(name)
        return ImproveOutcome(
            status=ImproveStatus.PROPOSE_FAILED, tool_name=name, failures=[str(e)]
        )

    new_sha = hash_source(proposal["source"])
    if new_sha == spec.sha256:
        # No-op proposal — model just echoed back the source. Don't burn
        # cycles testing it; treat as a soft failure.
        await _bump_consecutive_failures(name)
        return ImproveOutcome(status=ImproveStatus.NO_OP, tool_name=name)

    new_cases = _derive_test_cases(failures, proposal)

    # Build a candidate DynamicSpec and run the full test suite + new cases.
    candidate = DynamicSpec(
        name=spec.name,
        description=spec.description,
        input_schema=spec.input_schema,
        source=proposal["source"],
        sha256=new_sha,
        execution=spec.execution or "subprocess",
        scope=spec.scope,
        owner_session_id=spec.owner_session_id,
        version=spec.version,
        meta=spec.meta,
    )
    suite: list[TestCase] = [TestCase.from_spec(t.name, t.spec or {}) for t in tests]
    for case_name, case_spec in new_cases:
        suite.append(TestCase.from_spec(case_name, case_spec))

    results = await run_all(candidate, suite)
    n_pass = sum(1 for r in results if r.passed)
    n_fail = len(results) - n_pass

    if n_fail > 0:
        await _bump_consecutive_failures(name)
        return ImproveOutcome(
            status=ImproveStatus.TESTS_FAILED,
            tool_name=name,
            failures=[f"{r.case_name}: {'; '.join(r.failures)}" for r in results if not r.passed],
            n_existing_pass=n_pass,
            n_existing_fail=n_fail,
            n_new_cases=len(new_cases),
        )

    new_version = await _commit_new_version(
        name=name, proposal=proposal, spec=spec, new_cases=new_cases
    )
    return ImproveOutcome(
        status=ImproveStatus.PASSED,
        tool_name=name,
        new_version=new_version,
        rationale=str(proposal.get("rationale", "")) or None,
        n_existing_pass=n_pass,
        n_existing_fail=0,
        n_new_cases=len(new_cases),
    )
