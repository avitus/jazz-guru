"""Test-case runner for Tier-2 dynamic tools.

Wires a ``DynamicSpec`` through the existing subprocess sandbox in
``actions/dynamic.invoke`` and evaluates the result against a
:class:`TestCase`. Predicate evaluation reuses the predicate DSL in
``testing.predicates``; rubric evaluation reuses ``eval.judge`` when a
caller opts in by passing ``judge_task_label``.

The ``predicate_source`` escape hatch documented in the plan is NOT
implemented here yet — the DSL covers every case we have so far. Adding
it later only requires another small subprocess wrapper next to the
existing one in ``actions/dynamic._run_subprocess``.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from jazz_guru.actions.dynamic import DynamicSpec, invoke
from jazz_guru.testing import predicates

__all__ = [
    "TestCase",
    "TestRunResult",
    "run_all",
    "run_test_case",
]


@dataclass
class TestCase:
    """One executable case attached to a Tier-2 tool.

    ``predicate`` and ``rubric`` are both optional; a case must have at
    least one of them to be meaningful. If both are present, both must
    pass (AND-conjunction).
    """

    # Pytest auto-collects classes named ``Test*`` and tries to instantiate
    # them as test classes. This dataclass is a domain type, not a test —
    # tell pytest to skip it.
    __test__ = False

    name: str
    input: dict[str, Any]
    predicate: dict[str, Any] | None = None
    rubric: dict[str, Any] | None = None
    timeout_sec: int | None = None

    @classmethod
    def from_spec(cls, name: str, spec: dict[str, Any]) -> TestCase:
        """Parse a ``generated_tool_tests.spec`` JSON blob into a TestCase."""
        case = spec.get("case") or {}
        return cls(
            name=name,
            input=case.get("input") or {},
            predicate=case.get("predicate"),
            rubric=spec.get("rubric"),
            timeout_sec=spec.get("timeout_sec"),
        )


@dataclass
class TestRunResult:
    """Outcome of one ``run_test_case`` invocation.

    ``failures`` is a flat list of human-readable failure messages
    (predicate clauses that didn't hold + judge-below-threshold +
    subprocess errors). Empty iff ``passed`` is true.
    """

    __test__ = False

    case_name: str
    passed: bool
    output: Any = None
    error: str | None = None
    ms: int = 0
    judge_score: float | None = None
    failures: list[str] = field(default_factory=list)


async def run_test_case(
    spec: DynamicSpec,
    case: TestCase,
    *,
    judge_task_label: str | None = None,
) -> TestRunResult:
    """Invoke ``spec`` with ``case.input``, evaluate, return one result.

    The tool output is wrapped as ``{"result": output}`` before the
    predicate runs so paths like ``result.licks`` match the plan's
    canonical syntax. Subprocess-level errors surface as failures, not
    raised exceptions — the improvement loop expects every test outcome
    to be a structured record.

    ``judge_task_label`` is the human prompt passed to the rubric judge;
    when None and a case has a rubric, the rubric is skipped (predicate-
    only run). This lets the improver opt in to LLM cost case-by-case.
    """
    start = time.perf_counter()
    try:
        output = await invoke(spec, case.input)
    except Exception as e:
        return TestRunResult(
            case_name=case.name,
            passed=False,
            output=None,
            error=f"{type(e).__name__}: {e}",
            ms=int((time.perf_counter() - start) * 1000),
            failures=[f"invoke raised: {type(e).__name__}: {e}"],
        )
    ms = int((time.perf_counter() - start) * 1000)

    # ``dynamic._run_subprocess`` returns ``{"__error__": ...}`` on tool
    # crashes / timeouts. Surface that explicitly so it counts as a
    # failure rather than letting a predicate accidentally pass against
    # the error dict.
    if isinstance(output, dict) and "__error__" in output:
        return TestRunResult(
            case_name=case.name,
            passed=False,
            output=output,
            error=str(output["__error__"]),
            ms=ms,
            failures=[f"tool error: {output['__error__']}"],
        )

    failures: list[str] = []

    if case.predicate is not None:
        pr = predicates.evaluate({"result": output}, case.predicate)
        failures.extend(pr.failures)

    judge_score: float | None = None
    if case.rubric is not None and judge_task_label is not None:
        # Import lazily so non-rubric tests don't pay the eval import cost
        # and don't fail in environments without the LLM client wired up.
        from jazz_guru.eval.judge import judge as _judge

        criteria = case.rubric.get("criteria") or {}
        threshold = float(case.rubric.get("threshold", 0.5))
        try:
            jr = await _judge(
                task=judge_task_label,
                response=json.dumps(output, default=str),
                rubric={k: float(v) for k, v in criteria.items()},
                expected=case.rubric.get("prompt"),
            )
            judge_score = jr.weighted_total
            if judge_score < threshold:
                failures.append(
                    f"rubric: score {judge_score:.2f} below threshold {threshold:.2f}"
                )
        except Exception as e:
            failures.append(f"rubric judge failed: {type(e).__name__}: {e}")

    return TestRunResult(
        case_name=case.name,
        passed=not failures,
        output=output,
        error=None,
        ms=ms,
        judge_score=judge_score,
        failures=failures,
    )


async def run_all(
    spec: DynamicSpec,
    cases: list[TestCase],
    *,
    judge_task_label: str | None = None,
    concurrency: int = 4,
) -> list[TestRunResult]:
    """Run every case concurrently, bounded by ``concurrency``.

    Results come back in input order (asyncio.gather preserves order),
    not completion order, so callers can pair them with the input list.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(c: TestCase) -> TestRunResult:
        async with sem:
            return await run_test_case(spec, c, judge_task_label=judge_task_label)

    return list(await asyncio.gather(*(_bounded(c) for c in cases)))
