"""Add durable environment invalidations.

Revision ID: i05_environment_verify_v1
Revises: i05_environment_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i05_environment_verify_v1"
down_revision = "i05_environment_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "environment_invalidations",
        sa.Column("invalidation_id", sa.String(36), primary_key=True),
        sa.Column("binding_id", sa.String(36), unique=True, nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_environment_invalidation_recorded",
        "environment_invalidations",
        ["recorded_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_environment_invalidation_recorded",
        table_name="environment_invalidations",
    )
    op.drop_table("environment_invalidations")
