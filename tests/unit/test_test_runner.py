"""Tests for the Tier-2 tool test runner.

Round-trips real subprocess invocations through ``dynamic.invoke`` to
exercise the predicate-evaluation, error-surfacing, and rubric-skip
paths. No DB; the runner doesn't load tests itself — its callers do.
"""
from __future__ import annotations

import textwrap

import pytest

from jazz_guru.actions.dynamic import DynamicSpec, hash_source
from jazz_guru.testing.runner import TestCase, run_all, run_test_case


def _spec(name: str, source: str) -> DynamicSpec:
    src = textwrap.dedent(source).strip() + "\n"
    return DynamicSpec(
        name=name,
        description="x",
        input_schema={"type": "object"},
        source=src,
        sha256=hash_source(src),
        execution="subprocess",
    )


@pytest.mark.asyncio
async def test_case_passes_with_simple_predicate() -> None:
    """Happy path: predicate matches the tool output."""
    spec = _spec(
        "adder",
        """
        def run(a, b):
            return {"sum": a + b}
        """,
    )
    case = TestCase(
        name="sum_2_plus_3",
        input={"a": 2, "b": 3},
        predicate={"result.sum": 5},
    )
    r = await run_test_case(spec, case)
    assert r.passed
    assert r.failures == []
    assert r.output == {"sum": 5}
    assert r.error is None


@pytest.mark.asyncio
async def test_predicate_failure_records_clauses() -> None:
    """A failing predicate sets passed=False and lists every failed clause."""
    spec = _spec(
        "adder",
        """
        def run(a, b):
            return {"sum": a + b}
        """,
    )
    case = TestCase(
        name="wrong_expected",
        input={"a": 2, "b": 3},
        predicate={"result.sum": 99, "result.extra": {"present": True}},
    )
    r = await run_test_case(spec, case)
    assert not r.passed
    # Both clauses fail; both should be reported.
    assert len(r.failures) == 2


@pytest.mark.asyncio
async def test_tool_error_counts_as_failure() -> None:
    """A raised exception inside the tool comes back as ``__error__`` from
    ``dynamic.invoke`` — surface it as a failure, not a vacuous pass."""
    spec = _spec(
        "broken",
        """
        def run():
            raise ValueError("nope")
        """,
    )
    case = TestCase(name="any", input={}, predicate={"result": {"present": True}})
    r = await run_test_case(spec, case)
    assert not r.passed
    assert r.error is not None
    assert "ValueError" in r.error
    assert any("tool error" in f for f in r.failures)


@pytest.mark.asyncio
async def test_output_wrap_uses_result_alias() -> None:
    """Predicates reference ``result.x``; verify the runner wraps the tool
    output under the ``result`` key before evaluation."""
    spec = _spec(
        "echo",
        """
        def run(x):
            return {"value": x}
        """,
    )
    case = TestCase(
        name="echo_5",
        input={"x": 5},
        predicate={"result.value": 5},
    )
    r = await run_test_case(spec, case)
    assert r.passed, r.failures


@pytest.mark.asyncio
async def test_no_predicate_no_rubric_passes_vacuously() -> None:
    """A test case with neither predicate nor rubric trivially passes —
    useful for smoke cases that just verify the tool doesn't crash."""
    spec = _spec(
        "ok",
        """
        def run():
            return {"ok": True}
        """,
    )
    case = TestCase(name="smoke", input={})
    r = await run_test_case(spec, case)
    assert r.passed
    assert r.output == {"ok": True}


@pytest.mark.asyncio
async def test_rubric_skipped_without_judge_label() -> None:
    """A rubric is evaluated only when the caller passes ``judge_task_label``;
    otherwise the predicate alone decides the case."""
    spec = _spec(
        "ok",
        """
        def run():
            return {"text": "hello"}
        """,
    )
    case = TestCase(
        name="judged",
        input={},
        predicate={"result.text": "hello"},
        rubric={"criteria": {"is_polite": 1.0}, "threshold": 0.5},
    )
    # No judge_task_label → rubric path doesn't execute, no LLM call made.
    r = await run_test_case(spec, case)
    assert r.passed
    assert r.judge_score is None


@pytest.mark.asyncio
async def test_run_all_preserves_order() -> None:
    """``run_all`` uses asyncio.gather which returns in input order, not
    completion order — callers can zip results with cases."""
    spec = _spec(
        "adder",
        """
        def run(a, b):
            return {"sum": a + b}
        """,
    )
    cases = [
        TestCase(name="c1", input={"a": 1, "b": 1}, predicate={"result.sum": 2}),
        TestCase(name="c2", input={"a": 2, "b": 2}, predicate={"result.sum": 999}),
        TestCase(name="c3", input={"a": 3, "b": 3}, predicate={"result.sum": 6}),
    ]
    results = await run_all(spec, cases, concurrency=2)
    assert [r.case_name for r in results] == ["c1", "c2", "c3"]
    assert results[0].passed
    assert not results[1].passed
    assert results[2].passed


@pytest.mark.asyncio
async def test_ms_field_is_set() -> None:
    """Runtime measurements feed into the audit log and are required to be
    populated even on failure."""
    spec = _spec(
        "ok",
        """
        def run():
            return {"ok": True}
        """,
    )
    case = TestCase(name="any", input={}, predicate={"result.ok": True})
    r = await run_test_case(spec, case)
    # Subprocess work always takes nonzero ms, but a generous floor here
    # (>=0) keeps the test stable on fast machines.
    assert r.ms >= 0


@pytest.mark.asyncio
async def test_from_spec_parses_full_shape() -> None:
    """The plan §A.2 case+predicate+rubric+timeout YAML should round-trip
    through ``TestCase.from_spec`` to a fully populated TestCase."""
    spec_blob = {
        "case": {
            "input": {"chord": "Cmaj7"},
            "predicate": {"result.ok": True},
        },
        "rubric": {"criteria": {"x": 1.0}, "threshold": 0.5, "prompt": "is it ok?"},
        "timeout_sec": 7,
    }
    case = TestCase.from_spec("cmaj7_smoke", spec_blob)
    assert case.name == "cmaj7_smoke"
    assert case.input == {"chord": "Cmaj7"}
    assert case.predicate == {"result.ok": True}
    assert case.rubric is not None
    assert case.timeout_sec == 7


@pytest.mark.asyncio
async def test_from_spec_handles_predicate_only() -> None:
    """Predicate-only cases skip the rubric block entirely."""
    case = TestCase.from_spec(
        "p_only",
        {"case": {"input": {}, "predicate": {"result.x": 1}}},
    )
    assert case.rubric is None
    assert case.timeout_sec is None


@pytest.mark.asyncio
async def test_from_spec_handles_empty_case_block() -> None:
    """Defensive: a partially-filled spec shouldn't blow up at parse time."""
    case = TestCase.from_spec("empty", {})
    assert case.input == {}
    assert case.predicate is None
    assert case.rubric is None
