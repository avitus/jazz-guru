"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-04
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

from jazz_guru.config import get_settings

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    dim = get_settings().embedding_dim

    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("goal_profile", sa.String(64), nullable=False, server_default="default"),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "turns",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("session_id", sa.UUID(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_turns_session_id", "turns", ["session_id"])
    op.create_index("ix_turns_session_idx", "turns", ["session_id", "idx"])

    op.create_table(
        "events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("session_id", sa.UUID(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("turn_id", sa.UUID(), sa.ForeignKey("turns.id", ondelete="SET NULL"), nullable=True),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_events_session_id", "events", ["session_id"])
    op.create_index("ix_events_turn_id", "events", ["turn_id"])
    op.create_index("ix_events_type", "events", ["type"])
    op.create_index("ix_events_ts", "events", ["ts"])

    op.create_table(
        "snapshots",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("session_id", sa.UUID(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("turn_id", sa.UUID(), sa.ForeignKey("turns.id", ondelete="SET NULL"), nullable=True),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "memory_items",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("session_id", sa.UUID(), sa.ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("embedding", Vector(dim), nullable=True),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_memory_items_session_id", "memory_items", ["session_id"])
    op.create_index("ix_memory_items_kind", "memory_items", ["kind"])
    op.create_index("ix_memory_items_ts", "memory_items", ["ts"])
    op.execute(
        "CREATE INDEX ix_memory_items_embedding "
        "ON memory_items USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )

    op.create_table(
        "playbook_entries",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("meta", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_playbook_entries_scope", "playbook_entries", ["scope"])

    op.create_table(
        "eval_runs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.UUID(), sa.ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("rubric", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_eval_runs_task_id", "eval_runs", ["task_id"])
    op.create_index("ix_eval_runs_ts", "eval_runs", ["ts"])


def downgrade() -> None:
    op.drop_table("eval_runs")
    op.drop_table("playbook_entries")
    op.execute("DROP INDEX IF EXISTS ix_memory_items_embedding")
    op.drop_table("memory_items")
    op.drop_table("snapshots")
    op.drop_table("events")
    op.drop_table("turns")
    op.drop_table("sessions")
