"""Persist immutable change-set validation evidence.

Revision ID: i03_change_evidence
Revises: i04_artifact_reference_history
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i03_change_evidence"
down_revision = "i04_artifact_reference_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("change_sets", sa.Column("validation_payload", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("change_sets", "validation_payload")
