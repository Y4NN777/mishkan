"""Retain the previous working-reference identity.

Revision ID: i04_artifact_reference_history
Revises: i04_event_retention
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i04_artifact_reference_history"
down_revision = "i04_event_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("artifact_references") as batch:
        batch.add_column(sa.Column("prior_artifact_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("prior_revision", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("artifact_references") as batch:
        batch.drop_column("prior_revision")
        batch.drop_column("prior_artifact_id")
