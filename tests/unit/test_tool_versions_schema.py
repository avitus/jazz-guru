"""Schema-level tests for the Tier-2 tool version/test tables.

PR 1 of the plan in docs/plans/tier2-tool-tests-and-improvement.md is
schema-only: no business logic. These tests pin column types, default
values, and constraints so a later change can't silently drop them. Actual
DB round-trips happen in PR 2 once store.upsert/rollback land.
"""
from __future__ import annotations

import uuid

from jazz_guru.state import (
    GeneratedTool,
    GeneratedToolTest,
    GeneratedToolTestRun,
    GeneratedToolVersion,
)


def test_module_exports_all_three_models() -> None:
    import jazz_guru.state as st

    assert "GeneratedToolVersion" in st.__all__
    assert "GeneratedToolTest" in st.__all__
    assert "GeneratedToolTestRun" in st.__all__


def test_generated_tool_version_table_layout() -> None:
    t = GeneratedToolVersion.__table__
    assert t.name == "generated_tool_versions"
    expected_cols = {
        "id",
        "tool_id",
        "version",
        "source",
        "sha256",
        "input_schema",
        "description",
        "meta",
        "origin",
        "rationale",
        "superseded_at",
        "superseded_by",
        "created_at",
    }
    assert set(t.columns.keys()) == expected_cols
    # Single FK pointing at the parent row; CASCADE so removing a tool
    # doesn't strand its history.
    fks = list(t.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "generated_tools"
    assert fks[0].ondelete == "CASCADE"
    # (tool_id, version) uniqueness is the rollback contract: each version
    # number belongs to exactly one snapshot per tool.
    uniques = [
        c for c in t.constraints if c.__class__.__name__ == "UniqueConstraint"
    ]
    assert any(
        sorted(c.columns.keys()) == ["tool_id", "version"] for c in uniques
    )
    # Nullables that the design relies on.
    assert t.columns["superseded_at"].nullable is True
    assert t.columns["superseded_by"].nullable is True
    assert t.columns["rationale"].nullable is True


def test_generated_tool_test_table_layout() -> None:
    t = GeneratedToolTest.__table__
    assert t.name == "generated_tool_tests"
    expected_cols = {
        "id",
        "tool_id",
        "name",
        "spec",
        "origin",
        "enabled",
        "created_at",
    }
    assert set(t.columns.keys()) == expected_cols
    fks = list(t.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "generated_tools"
    assert fks[0].ondelete == "CASCADE"
    # (tool_id, name) uniqueness lets tool_test_add be idempotent.
    uniques = [
        c for c in t.constraints if c.__class__.__name__ == "UniqueConstraint"
    ]
    assert any(
        sorted(c.columns.keys()) == ["name", "tool_id"] for c in uniques
    )


def test_generated_tool_test_run_table_layout() -> None:
    t = GeneratedToolTestRun.__table__
    assert t.name == "generated_tool_test_runs"
    expected_cols = {
        "id",
        "tool_id",
        "tool_version",
        "test_id",
        "passed",
        "output",
        "error",
        "ms",
        "judge_score",
        "ran_at",
    }
    assert set(t.columns.keys()) == expected_cols
    # Two FKs: tool and test, both CASCADE. Dropping a test drops its
    # run history with it.
    fk_targets = {fk.column.table.name: fk.ondelete for fk in t.foreign_keys}
    assert fk_targets == {
        "generated_tools": "CASCADE",
        "generated_tool_tests": "CASCADE",
    }
    # Nullables for the failure path (no output, no judge score).
    assert t.columns["output"].nullable is True
    assert t.columns["error"].nullable is True
    assert t.columns["judge_score"].nullable is True
    # The (tool_id, ran_at) composite index is what "latest run per tool"
    # queries rely on.
    indexes = {ix.name: tuple(c.name for c in ix.columns) for ix in t.indexes}
    assert indexes.get("ix_generated_tool_test_runs_tool_ran_at") == (
        "tool_id",
        "ran_at",
    )


def test_models_construct_without_db() -> None:
    """Required fields accepted; nullable fields default to None pre-flush.

    Note: SQLAlchemy column-level ``default=`` values are applied at INSERT
    time, not at python construction. The defaults themselves are pinned in
    ``test_column_defaults`` below.
    """
    tool_id = uuid.uuid4()
    GeneratedToolVersion(
        tool_id=tool_id,
        version=1,
        source="def run():\n    return {}\n",
        sha256="abc",
        input_schema={"type": "object"},
        description="x",
    )
    GeneratedToolTest(
        tool_id=tool_id,
        name="case_a",
        spec={"case": {"input": {}, "predicate": {}}},
    )
    r = GeneratedToolTestRun(
        tool_id=tool_id,
        tool_version=1,
        test_id=uuid.uuid4(),
        passed=True,
    )
    # Failure-path fields are nullable; ensure construction leaves them unset.
    assert r.output is None
    assert r.error is None
    assert r.judge_score is None


def test_column_defaults() -> None:
    """Pin the column-level defaults so a later change can't silently drop them."""
    v_cols = GeneratedToolVersion.__table__.columns
    assert v_cols["origin"].default.arg == "manual"

    t_cols = GeneratedToolTest.__table__.columns
    assert t_cols["origin"].default.arg == "agent_authored"
    assert t_cols["enabled"].default.arg is True

    r_cols = GeneratedToolTestRun.__table__.columns
    assert r_cols["ms"].default.arg == 0


def test_generated_tool_still_imports() -> None:
    """Smoke test: the existing model is untouched by PR 1."""
    assert GeneratedTool.__tablename__ == "generated_tools"
