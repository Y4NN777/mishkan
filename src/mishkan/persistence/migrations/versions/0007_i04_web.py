"""Add persistent Web cache entries.

Revision ID: i04_web
Revises: i03_runtime
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i04_web"
down_revision = "i03_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_cache_entries",
        sa.Column("key", sa.String(71), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("stored_at", sa.String(40), nullable=False),
        sa.Column("fresh_until", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_web_cache_kind_freshness",
        "web_cache_entries",
        ["kind", "fresh_until"],
    )


def downgrade() -> None:
    op.drop_index("ix_web_cache_kind_freshness", table_name="web_cache_entries")
    op.drop_table("web_cache_entries")
