"""tool versions and tests

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-12
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "generated_tool_versions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "tool_id",
            sa.UUID(),
            sa.ForeignKey("generated_tools.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column(
            "input_schema", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("origin", sa.String(32), nullable=False, server_default="manual"),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tool_id", "version", name="uq_generated_tool_versions_tool_version"
        ),
    )
    op.create_index(
        "ix_generated_tool_versions_tool_id", "generated_tool_versions", ["tool_id"]
    )

    op.create_table(
        "generated_tool_tests",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "tool_id",
            sa.UUID(),
            sa.ForeignKey("generated_tools.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(96), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column(
            "origin", sa.String(32), nullable=False, server_default="agent_authored"
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tool_id", "name", name="uq_generated_tool_tests_tool_name"),
    )
    op.create_index("ix_generated_tool_tests_tool_id", "generated_tool_tests", ["tool_id"])

    op.create_table(
        "generated_tool_test_runs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "tool_id",
            sa.UUID(),
            sa.ForeignKey("generated_tools.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_version", sa.Integer(), nullable=False),
        sa.Column(
            "test_id",
            sa.UUID(),
            sa.ForeignKey("generated_tool_tests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("judge_score", sa.Float(), nullable=True),
        sa.Column(
            "ran_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_generated_tool_test_runs_tool_id", "generated_tool_test_runs", ["tool_id"]
    )
    op.create_index(
        "ix_generated_tool_test_runs_test_id", "generated_tool_test_runs", ["test_id"]
    )
    op.create_index(
        "ix_generated_tool_test_runs_ran_at", "generated_tool_test_runs", ["ran_at"]
    )
    op.create_index(
        "ix_generated_tool_test_runs_tool_ran_at",
        "generated_tool_test_runs",
        ["tool_id", "ran_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_generated_tool_test_runs_tool_ran_at", table_name="generated_tool_test_runs"
    )
    op.drop_index(
        "ix_generated_tool_test_runs_ran_at", table_name="generated_tool_test_runs"
    )
    op.drop_index(
        "ix_generated_tool_test_runs_test_id", table_name="generated_tool_test_runs"
    )
    op.drop_index(
        "ix_generated_tool_test_runs_tool_id", table_name="generated_tool_test_runs"
    )
    op.drop_table("generated_tool_test_runs")

    op.drop_index("ix_generated_tool_tests_tool_id", table_name="generated_tool_tests")
    op.drop_table("generated_tool_tests")

    op.drop_index(
        "ix_generated_tool_versions_tool_id", table_name="generated_tool_versions"
    )
    op.drop_table("generated_tool_versions")
