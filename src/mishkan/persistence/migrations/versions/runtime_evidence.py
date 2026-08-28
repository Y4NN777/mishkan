"""Add durable rejected-review evidence.

Revision ID: i04_runtime_evidence
Revises: i04_artifact_reconciliation
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i04_runtime_evidence"
down_revision = "i04_artifact_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_review_rejections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("task_key", sa.String(64), nullable=False),
        sa.Column("task_attempt", sa.Integer(), nullable=False),
        sa.Column("review_sequence", sa.Integer(), nullable=False),
        sa.Column("result_payload", sa.Text(), nullable=False),
        sa.Column("review_payload", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.String(40), nullable=False),
        sa.UniqueConstraint(
            "run_id",
            "task_key",
            "task_attempt",
            "review_sequence",
            name="uq_task_review_rejection_identity",
        ),
    )


def downgrade() -> None:
    op.drop_table("task_review_rejections")
