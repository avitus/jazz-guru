"""Tests for the predicate DSL used by Tier-2 tool test cases.

Pure logic — no DB, no subprocess. The runner (PR 4) is what wires this
into the test-case execution pipeline; here we only verify the DSL
itself behaves predictably across operators, paths, and quantifiers.
"""
from __future__ import annotations

import pytest

from jazz_guru.testing.predicates import (
    PredicateError,
    PredicateResult,
    evaluate,
)

# ---------------------------------------------------------------------- path


def test_path_simple_identifier() -> None:
    """A single-segment path resolves a top-level key."""
    r = evaluate({"x": 1}, {"x": 1})
    assert r.passed


def test_path_dotted() -> None:
    r = evaluate({"a": {"b": {"c": 3}}}, {"a.b.c": 3})
    assert r.passed


def test_path_indexed() -> None:
    r = evaluate({"a": [10, 20, 30]}, {"a[1]": 20})
    assert r.passed


def test_path_negative_index() -> None:
    r = evaluate({"a": [10, 20, 30]}, {"a[-1]": 30})
    assert r.passed


def test_path_index_out_of_range_is_missing() -> None:
    r = evaluate({"a": [10]}, {"a[5]": {"absent": True}})
    assert r.passed


def test_path_invalid_index_raises() -> None:
    with pytest.raises(PredicateError):
        evaluate({"a": [1]}, {"a[xyz]": 1})


def test_path_unmatched_bracket_raises() -> None:
    with pytest.raises(PredicateError):
        evaluate({"a": [1]}, {"a[0": 1})


def test_path_empty_raises() -> None:
    with pytest.raises(PredicateError):
        evaluate({}, {"": 1})


def test_path_leading_dot_raises() -> None:
    with pytest.raises(PredicateError):
        evaluate({"a": 1}, {".a": 1})


def test_path_non_string_key_raises() -> None:
    with pytest.raises(PredicateError):
        # Non-string key is caller bug, not just a bad path.
        evaluate({"a": 1}, {1: 1})  # type: ignore[dict-item]


# ---------------------------------------------------------------------- eq/ne


def test_implicit_eq_pass() -> None:
    r = evaluate({"x": "y"}, {"x": "y"})
    assert r.passed


def test_implicit_eq_fail_includes_actual() -> None:
    r = evaluate({"x": "y"}, {"x": "z"})
    assert not r.passed
    assert any("expected 'z'" in f and "got 'y'" in f for f in r.failures)


def test_explicit_eq() -> None:
    r = evaluate({"x": 1}, {"x": {"eq": 1}})
    assert r.passed
    r = evaluate({"x": 1}, {"x": {"eq": 2}})
    assert not r.passed


def test_ne() -> None:
    r = evaluate({"x": 1}, {"x": {"ne": 2}})
    assert r.passed
    r = evaluate({"x": 1}, {"x": {"ne": 1}})
    assert not r.passed


# ---------------------------------------------------------------------- gt/lt


@pytest.mark.parametrize(
    "op,actual,expected,should_pass",
    [
        ("gt", 5, 4, True),
        ("gt", 5, 5, False),
        ("gte", 5, 5, True),
        ("gte", 4, 5, False),
        ("lt", 4, 5, True),
        ("lt", 5, 5, False),
        ("lte", 5, 5, True),
        ("lte", 6, 5, False),
    ],
)
def test_numeric_comparators(op: str, actual: int, expected: int, should_pass: bool) -> None:
    r = evaluate({"x": actual}, {"x": {op: expected}})
    assert r.passed is should_pass


def test_comparator_type_mismatch_fails_cleanly() -> None:
    """Comparing str < int returns False rather than crashing."""
    r = evaluate({"x": "a"}, {"x": {"lt": 5}})
    assert not r.passed


# ---------------------------------------------------------------------- len


def test_len_scalar() -> None:
    r = evaluate({"items": [1, 2, 3]}, {"items": {"len": 3}})
    assert r.passed


def test_len_nested_ops() -> None:
    r = evaluate({"items": [1, 2, 3]}, {"items": {"len": {"gte": 2, "lt": 10}}})
    assert r.passed


def test_len_on_string() -> None:
    r = evaluate({"s": "hello"}, {"s": {"len": 5}})
    assert r.passed


def test_len_on_non_sized_fails() -> None:
    r = evaluate({"n": 42}, {"n": {"len": 1}})
    assert not r.passed
    assert "no len()" in r.failures[0]


def test_len_bool_arg_is_malformed() -> None:
    """``{len: true}`` is a bug, not "length equals 1"."""
    with pytest.raises(PredicateError):
        evaluate({"a": [1]}, {"a": {"len": True}})


# ---------------------------------------------------------------- contains


def test_contains_substring() -> None:
    r = evaluate({"s": "hello world"}, {"s": {"contains": "world"}})
    assert r.passed


def test_contains_list_membership() -> None:
    r = evaluate({"a": [1, 2, 3]}, {"a": {"contains": 2}})
    assert r.passed
    r = evaluate({"a": [1, 2, 3]}, {"a": {"contains": 5}})
    assert not r.passed


def test_contains_list_all_members() -> None:
    """Passing a list to ``contains`` requires every member to be present."""
    r = evaluate({"a": [1, 2, 3]}, {"a": {"contains": [1, 3]}})
    assert r.passed
    r = evaluate({"a": [1, 2, 3]}, {"a": {"contains": [1, 9]}})
    assert not r.passed
    assert "missing items" in r.failures[0]


def test_contains_on_string_with_non_string_raises() -> None:
    with pytest.raises(PredicateError):
        evaluate({"s": "hello"}, {"s": {"contains": 5}})


# ---------------------------------------------------------------- regex


def test_regex_match() -> None:
    r = evaluate({"s": "Cmaj7"}, {"s": {"regex": r"^C"}})
    assert r.passed


def test_regex_no_match() -> None:
    r = evaluate({"s": "Cmaj7"}, {"s": {"regex": r"^D"}})
    assert not r.passed


def test_regex_against_non_string_fails() -> None:
    r = evaluate({"s": 5}, {"s": {"regex": r"^5$"}})
    # The DSL doesn't coerce — non-string values fail regex matching
    # cleanly with a typed error message instead of silently passing.
    assert not r.passed


def test_regex_invalid_pattern_raises() -> None:
    with pytest.raises(PredicateError):
        evaluate({"s": "x"}, {"s": {"regex": r"["}})


# ---------------------------------------------------------------- type


@pytest.mark.parametrize(
    "value,type_name,should_pass",
    [
        ("hi", "string", True),
        (1, "string", False),
        (1, "int", True),
        (True, "int", False),  # bool is excluded from int
        (1.5, "float", True),
        (1, "float", False),
        (1, "number", True),
        (1.5, "number", True),
        ("x", "number", False),
        # bool is a subclass of int; tuple targets must reject it too.
        (True, "number", False),
        (False, "number", False),
        (True, "bool", True),
        (1, "bool", False),  # int != bool
        ([1, 2], "array", True),
        ({}, "object", True),
        (None, "null", True),
    ],
)
def test_type_op(value: object, type_name: str, should_pass: bool) -> None:
    r = evaluate({"x": value}, {"x": {"type": type_name}})
    assert r.passed is should_pass


def test_type_unknown_raises() -> None:
    with pytest.raises(PredicateError):
        evaluate({"x": 1}, {"x": {"type": "weird"}})


# ---------------------------------------------------------- absent/present


def test_absent_true_on_missing() -> None:
    r = evaluate({"a": 1}, {"b": {"absent": True}})
    assert r.passed


def test_absent_true_on_present_fails() -> None:
    r = evaluate({"a": 1}, {"a": {"absent": True}})
    assert not r.passed


def test_present_on_existing() -> None:
    r = evaluate({"a": 1}, {"a": {"present": True}})
    assert r.passed


def test_present_on_missing_fails() -> None:
    r = evaluate({"a": 1}, {"b": {"present": True}})
    assert not r.passed


def test_present_false_inverts() -> None:
    r = evaluate({"a": 1}, {"b": {"present": False}})
    assert r.passed


def test_absent_with_non_bool_arg_raises() -> None:
    with pytest.raises(PredicateError):
        evaluate({}, {"x": {"absent": "yes"}})


# ---------------------------------------------------- missing-value handling


def test_missing_path_with_eq_fails_clearly() -> None:
    """Eq against a missing path fails with a path-missing message, not a
    comparison of None against the expected value."""
    r = evaluate({"a": 1}, {"missing.deep.path": "x"})
    assert not r.passed
    assert "missing" in " ".join(r.failures).lower()


def test_missing_path_with_combined_absent_passes() -> None:
    r = evaluate({"a": 1}, {"b": {"absent": True, "type": "string"}})
    # Combined ops with absent on a missing path: absent succeeds; type
    # is irrelevant since we never got a value.
    assert r.passed


# ---------------------------------------------------------------- all/any


def test_all_pass() -> None:
    r = evaluate({"items": [1, 2, 3]}, {"items": {"all": {"gt": 0}}})
    assert r.passed


def test_all_fail_reports_index() -> None:
    r = evaluate({"items": [1, -2, 3]}, {"items": {"all": {"gt": 0}}})
    assert not r.passed
    assert any("[1]" in f for f in r.failures)


def test_any_pass_short_circuits() -> None:
    r = evaluate({"items": [1, 2, 3]}, {"items": {"any": {"eq": 2}}})
    assert r.passed


def test_any_empty_list_fails() -> None:
    r = evaluate({"items": []}, {"items": {"any": {"eq": 1}}})
    assert not r.passed


def test_all_on_non_list_fails() -> None:
    r = evaluate({"x": "hi"}, {"x": {"all": {"eq": "h"}}})
    assert not r.passed


# ------------------------------------------------------ quantifier [*]


def test_star_each_element() -> None:
    """``a[*]`` applies the expectation to each element of ``a``."""
    r = evaluate(
        {"licks": [{"chord": "Cmaj7"}, {"chord": "Cmaj7"}]},
        {"licks[*].chord": "Cmaj7"},
    )
    assert r.passed


def test_star_one_bad_element_fails_with_index() -> None:
    r = evaluate(
        {"licks": [{"chord": "Cmaj7"}, {"chord": "Dm7"}]},
        {"licks[*].chord": "Cmaj7"},
    )
    assert not r.passed
    assert any("[1]" in f for f in r.failures)


def test_star_on_non_list_fails() -> None:
    r = evaluate({"licks": "not a list"}, {"licks[*].chord": "x"})
    assert not r.passed
    assert "list" in r.failures[0]


def test_star_on_missing_container_fails() -> None:
    r = evaluate({}, {"licks[*].chord": "x"})
    assert not r.passed
    assert "missing" in r.failures[0]


def test_nested_star_quantifiers() -> None:
    """``a[*].b[*].c`` should iterate both levels."""
    root = {"a": [{"b": [{"c": 1}, {"c": 1}]}, {"b": [{"c": 1}]}]}
    r = evaluate(root, {"a[*].b[*].c": 1})
    assert r.passed

    root_bad = {"a": [{"b": [{"c": 1}, {"c": 2}]}]}
    r = evaluate(root_bad, {"a[*].b[*].c": 1})
    assert not r.passed


# ---------------------------------------------------- conjunction & errors


def test_multi_clause_and_conjunction() -> None:
    """Every clause must pass; failures from each are reported."""
    root = {"x": 1, "y": "hi"}
    r = evaluate(root, {"x": 1, "y": "hi"})
    assert r.passed

    r = evaluate(root, {"x": 1, "y": "bye"})
    assert not r.passed
    # All failing clauses get reported, not just the first.
    assert any("y" in f for f in r.failures)


def test_multi_clause_collects_all_failures() -> None:
    """Predicates report every failed clause so the user fixes them all
    at once instead of one-per-rerun."""
    root = {"a": 1, "b": 2}
    r = evaluate(root, {"a": 99, "b": 99})
    assert not r.passed
    assert len(r.failures) == 2


def test_combined_ops_in_one_clause_are_anded() -> None:
    r = evaluate({"x": 5}, {"x": {"gt": 0, "lt": 10, "type": "int"}})
    assert r.passed
    r = evaluate({"x": 5}, {"x": {"gt": 0, "lt": 3}})
    assert not r.passed


def test_unknown_operator_raises() -> None:
    with pytest.raises(PredicateError):
        evaluate({"x": 1}, {"x": {"is_prime": True}})


def test_unknown_operator_raises_even_on_missing_path() -> None:
    """A typo'd op alongside ``absent: True`` should still raise — silently
    dropping unknown keys would mask predicate-authoring bugs."""
    with pytest.raises(PredicateError):
        evaluate({}, {"missing": {"absent": True, "typoed_op": 1}})


def test_predicate_must_be_dict() -> None:
    with pytest.raises(PredicateError):
        evaluate({}, ["not", "a", "dict"])  # type: ignore[arg-type]


# ----------------------------------------------------------- happy path


def test_plan_example_predicate() -> None:
    """The exact predicate shape from the plan §A.2 example evaluates."""
    root = {
        "result": {
            "licks": [
                {"chord": "Cmaj7", "style": "bebop"},
                {"chord": "Cmaj7", "style": "bebop"},
                {"chord": "Cmaj7", "style": "bebop"},
            ],
        },
    }
    predicate = {
        "result.licks": {"len": 3},
        "result.licks[0].chord": "Cmaj7",
        "result.licks[*].style": {"eq": "bebop"},
        "result.error": {"absent": True},
    }
    result = evaluate(root, predicate)
    assert result.passed, result.failures


def test_predicate_result_is_dataclass() -> None:
    r = PredicateResult(passed=True)
    assert r.passed is True
    assert r.failures == []
