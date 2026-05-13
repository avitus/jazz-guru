"""Deterministic predicate DSL for Tier-2 tool tests.

Predicates are JSON-tree assertions used in ``generated_tool_tests.spec``.
A predicate is a dict mapping JSONPath-subset strings to expectations:

    {
      "result.licks":          {"len": 3},
      "result.licks[0].chord": "Cmaj7",
      "result.licks[*].style": {"eq": "bebop"},
      "result.error":          {"absent": true},
    }

Path syntax: dotted identifiers, ``[N]`` for list index, ``[*]`` for
"each element" (introduces an implicit all-quantifier over the array).

Expectations are either bare scalars (implicit ``eq``) or dicts of
operators (AND-conjunction). Operators: ``eq``, ``ne``, ``gt``, ``gte``,
``lt``, ``lte``, ``len`` (int or nested op-dict), ``contains``, ``regex``,
``type``, ``absent``, ``present``, ``all``, ``any``.

No ``eval``; no operator can run arbitrary code. The only escape hatch
for richer logic is ``predicate_source`` on a test case (handled by the
runner, not here), which runs in the same subprocess sandbox as the tool.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = ["PredicateError", "PredicateResult", "evaluate"]


class PredicateError(ValueError):
    """Raised on a malformed predicate (bad path, unknown operator, ...).

    A *failed* predicate (path resolved, expectation didn't hold) is NOT
    an error; it is a normal ``PredicateResult(passed=False, ...)``.
    """


@dataclass
class PredicateResult:
    passed: bool
    failures: list[str] = field(default_factory=list)


_MISSING = object()


# ---------- path parsing --------------------------------------------------


def _parse_path(path: str) -> list[str | int]:
    """Tokenize ``'a.b[0].c[*].d'`` into ``['a', 'b', 0, 'c', '*', 'd']``.

    Raises ``PredicateError`` on syntactic problems. Empty paths are
    rejected; a path like ``'result'`` (single identifier) is valid and
    resolves to the root's ``result`` key.
    """
    if not path:
        raise PredicateError("empty path")
    parts: list[str | int] = []
    i = 0
    n = len(path)
    while i < n:
        ch = path[i]
        if ch == ".":
            if not parts:
                raise PredicateError(f"path {path!r}: cannot start with '.'")
            i += 1
            j = i
            while j < n and (path[j].isalnum() or path[j] == "_"):
                j += 1
            if j == i:
                raise PredicateError(f"path {path!r}: expected identifier after '.'")
            parts.append(path[i:j])
            i = j
        elif ch == "[":
            close = path.find("]", i)
            if close == -1:
                raise PredicateError(f"path {path!r}: unmatched '['")
            inside = path[i + 1 : close]
            if inside == "*":
                parts.append("*")
            else:
                try:
                    parts.append(int(inside))
                except ValueError:
                    raise PredicateError(
                        f"path {path!r}: index {inside!r} is not '*' or an integer"
                    ) from None
            i = close + 1
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (path[j].isalnum() or path[j] == "_"):
                j += 1
            parts.append(path[i:j])
            i = j
        else:
            raise PredicateError(
                f"path {path!r}: unexpected character {ch!r} at index {i}"
            )
    return parts


# ---------- resolver ------------------------------------------------------


def _resolve(obj: Any, parts: list[str | int]) -> Any:
    """Walk ``parts`` through ``obj`` returning the value or ``_MISSING``.

    Caller must have split out any ``*`` quantifiers first.
    """
    for p in parts:
        if isinstance(p, str):
            if not isinstance(obj, dict) or p not in obj:
                return _MISSING
            obj = obj[p]
        elif isinstance(p, int):
            if not isinstance(obj, list):
                return _MISSING
            # Negative indices are supported as plain python list indexing.
            if p >= len(obj) or p < -len(obj):
                return _MISSING
            obj = obj[p]
        else:  # pragma: no cover - guarded by _parse_path
            raise PredicateError(f"internal: unexpected path segment {p!r}")
    return obj


# ---------- operators -----------------------------------------------------


def _op_eq(actual: Any, expected: Any) -> PredicateResult:
    if actual == expected:
        return PredicateResult(True)
    return PredicateResult(False, [f"expected {expected!r}, got {actual!r}"])


def _op_ne(actual: Any, expected: Any) -> PredicateResult:
    if actual != expected:
        return PredicateResult(True)
    return PredicateResult(False, [f"expected != {expected!r}, got {actual!r}"])


def _op_gt(actual: Any, expected: Any) -> PredicateResult:
    try:
        if actual > expected:
            return PredicateResult(True)
    except TypeError as e:
        return PredicateResult(False, [f"gt: {e}"])
    return PredicateResult(False, [f"expected > {expected!r}, got {actual!r}"])


def _op_gte(actual: Any, expected: Any) -> PredicateResult:
    try:
        if actual >= expected:
            return PredicateResult(True)
    except TypeError as e:
        return PredicateResult(False, [f"gte: {e}"])
    return PredicateResult(False, [f"expected >= {expected!r}, got {actual!r}"])


def _op_lt(actual: Any, expected: Any) -> PredicateResult:
    try:
        if actual < expected:
            return PredicateResult(True)
    except TypeError as e:
        return PredicateResult(False, [f"lt: {e}"])
    return PredicateResult(False, [f"expected < {expected!r}, got {actual!r}"])


def _op_lte(actual: Any, expected: Any) -> PredicateResult:
    try:
        if actual <= expected:
            return PredicateResult(True)
    except TypeError as e:
        return PredicateResult(False, [f"lte: {e}"])
    return PredicateResult(False, [f"expected <= {expected!r}, got {actual!r}"])


def _op_len(actual: Any, expected: Any) -> PredicateResult:
    try:
        actual_len = len(actual)
    except TypeError:
        return PredicateResult(
            False, [f"value of type {type(actual).__name__} has no len()"]
        )
    if isinstance(expected, bool):
        # bool is a subclass of int — guard explicitly so {len: true} surfaces
        # as malformed instead of being silently treated as len == 1.
        raise PredicateError("len expectation must be int or dict, not bool")
    if isinstance(expected, int):
        if actual_len == expected:
            return PredicateResult(True)
        return PredicateResult(False, [f"expected len == {expected}, got {actual_len}"])
    if isinstance(expected, dict):
        return _check_op(actual_len, expected)
    raise PredicateError(
        f"len expectation must be int or dict, got {type(expected).__name__}"
    )


def _op_contains(actual: Any, expected: Any) -> PredicateResult:
    if isinstance(actual, str):
        if not isinstance(expected, str):
            raise PredicateError(
                f"contains on string needs string arg, got {type(expected).__name__}"
            )
        if expected in actual:
            return PredicateResult(True)
        return PredicateResult(False, [f"string {actual!r} does not contain {expected!r}"])
    if isinstance(actual, (list, tuple, set)):
        if isinstance(expected, list):
            missing = [x for x in expected if x not in actual]
            if not missing:
                return PredicateResult(True)
            return PredicateResult(False, [f"missing items: {missing!r}"])
        if expected in actual:
            return PredicateResult(True)
        return PredicateResult(False, [f"does not contain {expected!r}"])
    return PredicateResult(
        False, [f"contains on type {type(actual).__name__} not supported"]
    )


def _op_regex(actual: Any, expected: Any) -> PredicateResult:
    if not isinstance(expected, str):
        raise PredicateError("regex pattern must be a string")
    if not isinstance(actual, str):
        return PredicateResult(
            False, [f"regex needs string value, got {type(actual).__name__}"]
        )
    try:
        if re.search(expected, actual):
            return PredicateResult(True)
    except re.error as e:
        raise PredicateError(f"invalid regex {expected!r}: {e}") from e
    return PredicateResult(False, [f"value {actual!r} does not match /{expected}/"])


_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "str": str,
    "int": int,
    "integer": int,
    "float": float,
    "number": (int, float),
    "bool": bool,
    "boolean": bool,
    "array": list,
    "list": list,
    "object": dict,
    "dict": dict,
    "null": type(None),
}


def _op_type(actual: Any, expected: Any) -> PredicateResult:
    if not isinstance(expected, str) or expected not in _TYPE_MAP:
        raise PredicateError(
            f"type expectation must be one of {sorted(_TYPE_MAP)}, got {expected!r}"
        )
    target = _TYPE_MAP[expected]
    # ``bool`` is a subclass of ``int`` in python. Tighten both directions:
    # ``int`` rejects bools; ``bool`` accepts only true bools (not other ints).
    if target is int:
        if isinstance(actual, bool) or not isinstance(actual, int):
            return PredicateResult(
                False, [f"expected type int, got {type(actual).__name__}"]
            )
        return PredicateResult(True)
    if target is bool:
        if isinstance(actual, bool):
            return PredicateResult(True)
        return PredicateResult(
            False, [f"expected type bool, got {type(actual).__name__}"]
        )
    # Tuple targets (e.g. ``"number"`` → ``(int, float)``) bypass the
    # ``target is int`` guard above, so apply the bool-rejection here too.
    # Without this, ``type: "number"`` accepts ``True``/``False`` because
    # ``isinstance(True, (int, float))`` is True.
    if isinstance(target, tuple) and int in target and isinstance(actual, bool):
        return PredicateResult(
            False, [f"expected type {expected}, got {type(actual).__name__}"]
        )
    if isinstance(actual, target):
        return PredicateResult(True)
    return PredicateResult(
        False, [f"expected type {expected}, got {type(actual).__name__}"]
    )


def _op_absent(actual: Any, expected: Any) -> PredicateResult:
    if not isinstance(expected, bool):
        raise PredicateError(
            f"absent expectation must be bool, got {type(expected).__name__}"
        )
    is_absent = actual is _MISSING
    if expected:
        if is_absent:
            return PredicateResult(True)
        return PredicateResult(False, [f"expected absent, got {actual!r}"])
    if not is_absent:
        return PredicateResult(True)
    return PredicateResult(False, ["expected present, got missing"])


def _op_present(actual: Any, expected: Any) -> PredicateResult:
    if not isinstance(expected, bool):
        raise PredicateError(
            f"present expectation must be bool, got {type(expected).__name__}"
        )
    # Symmetric counterpart to ``absent``.
    return _op_absent(actual, not expected)


def _op_all(actual: Any, expectation: Any) -> PredicateResult:
    if not isinstance(actual, list):
        return PredicateResult(
            False, [f"all requires list, got {type(actual).__name__}"]
        )
    failures: list[str] = []
    for i, item in enumerate(actual):
        r = _check_op(item, expectation)
        if not r.passed:
            failures.extend(f"[{i}]: {f}" for f in r.failures)
    return PredicateResult(not failures, failures)


def _op_any(actual: Any, expectation: Any) -> PredicateResult:
    if not isinstance(actual, list):
        return PredicateResult(
            False, [f"any requires list, got {type(actual).__name__}"]
        )
    if not actual:
        return PredicateResult(False, ["any: empty list"])
    for item in actual:
        r = _check_op(item, expectation)
        if r.passed:
            return PredicateResult(True)
    return PredicateResult(False, [f"any: no element satisfied {expectation!r}"])


_OPS = {
    "eq": _op_eq,
    "ne": _op_ne,
    "gt": _op_gt,
    "gte": _op_gte,
    "lt": _op_lt,
    "lte": _op_lte,
    "len": _op_len,
    "contains": _op_contains,
    "regex": _op_regex,
    "type": _op_type,
    "absent": _op_absent,
    "present": _op_present,
    "all": _op_all,
    "any": _op_any,
}


# ---------- core ----------------------------------------------------------


def _check_op(actual: Any, expectation: Any) -> PredicateResult:
    """Evaluate one expectation against one resolved value.

    Bare scalars become implicit ``eq``. A dict expectation is an
    AND-conjunction of operators. Missing values only make sense to
    ``absent`` / ``present``; everything else fails fast with a clear
    message rather than letting NoneType-like comparisons leak through.
    """
    # Operator validation runs FIRST for dict expectations — independent of
    # whether the path resolved — so a typo'd op like ``{absent: True,
    # typoed: 1}`` surfaces as a malformed-predicate error rather than
    # being silently dropped on the missing-path branch below.
    if isinstance(expectation, dict):
        for op_name in expectation:
            if op_name not in _OPS:
                raise PredicateError(f"unknown operator {op_name!r}")

    if actual is _MISSING:
        if isinstance(expectation, dict):
            # Only absent/present can meaningfully consume a missing value;
            # everything else fails the clause because the value isn't there
            # to evaluate against.
            relevant = {k: v for k, v in expectation.items() if k in ("absent", "present")}
            if not relevant:
                return PredicateResult(
                    False,
                    [f"path missing; expectation requires {list(expectation.keys())}"],
                )
            failures: list[str] = []
            for op_name, arg in relevant.items():
                r = _OPS[op_name](actual, arg)
                if not r.passed:
                    failures.extend(r.failures)
            return PredicateResult(not failures, failures)
        return PredicateResult(False, [f"path missing; expected {expectation!r}"])

    if not isinstance(expectation, dict):
        return _op_eq(actual, expectation)

    failures = []
    for op_name, arg in expectation.items():
        r = _OPS[op_name](actual, arg)
        if not r.passed:
            failures.extend(r.failures)
    return PredicateResult(not failures, failures)


def _evaluate_path(root: Any, parts: list[str | int], expectation: Any) -> PredicateResult:
    """Walk ``parts`` over ``root``; on ``*``, recurse element-wise."""
    for i, p in enumerate(parts):
        if p == "*":
            container = _resolve(root, parts[:i])
            if container is _MISSING:
                return PredicateResult(False, ["container missing before [*]"])
            if not isinstance(container, list):
                return PredicateResult(
                    False, [f"expected list at [*], got {type(container).__name__}"]
                )
            failures: list[str] = []
            for idx, item in enumerate(container):
                sub = _evaluate_path(item, parts[i + 1 :], expectation)
                if not sub.passed:
                    failures.extend(f"[{idx}]: {f}" for f in sub.failures)
            return PredicateResult(not failures, failures)
    value = _resolve(root, parts)
    return _check_op(value, expectation)


def evaluate(root: Any, predicate: dict[str, Any]) -> PredicateResult:
    """Run every clause of ``predicate`` against ``root`` (AND-conjunction).

    Each clause is one ``{path: expectation}`` pair. All clause failures
    are accumulated into the returned ``PredicateResult`` so the caller
    can show every problem at once instead of one-at-a-time.
    """
    if not isinstance(predicate, dict):
        raise PredicateError("predicate must be a dict")
    all_failures: list[str] = []
    for path, expectation in predicate.items():
        if not isinstance(path, str):
            raise PredicateError(f"predicate path must be a string, got {path!r}")
        parts = _parse_path(path)
        result = _evaluate_path(root, parts, expectation)
        for f in result.failures:
            all_failures.append(f"{path}: {f}")
    return PredicateResult(not all_failures, all_failures)
