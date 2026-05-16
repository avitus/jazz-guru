"""turns content trigram index

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-11
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # pg_trgm is required for the GIN index on the cast(content as text)
    # expression below. Idempotent; harmless if already installed.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_turns_content_trgm "
        "ON turns USING GIN ((content::text) gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_turns_content_trgm")
    # Intentionally NOT dropping the extension: other tables may use it.
