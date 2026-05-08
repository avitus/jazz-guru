"""generated tools

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "generated_tools",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(96), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("input_schema", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False, server_default="global"),
        sa.Column("owner_session_id", sa.UUID(), sa.ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("deprecated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("meta", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_generated_tools_name"),
    )
    op.create_index("ix_generated_tools_name", "generated_tools", ["name"])
    op.create_index("ix_generated_tools_scope", "generated_tools", ["scope"])
    op.create_index("ix_generated_tools_owner_session_id", "generated_tools", ["owner_session_id"])


def downgrade() -> None:
    op.drop_index("ix_generated_tools_owner_session_id", table_name="generated_tools")
    op.drop_index("ix_generated_tools_scope", table_name="generated_tools")
    op.drop_index("ix_generated_tools_name", table_name="generated_tools")
    op.drop_table("generated_tools")
