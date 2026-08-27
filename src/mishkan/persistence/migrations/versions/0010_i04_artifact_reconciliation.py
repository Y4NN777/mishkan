"""Add durable artifact reconciliation plans.

Revision ID: i04_artifact_reconciliation
Revises: i04_mcp
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i04_artifact_reconciliation"
down_revision = "i04_mcp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifact_reconciliation_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("applied_at", sa.String(40), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("artifact_reconciliation_plans")
