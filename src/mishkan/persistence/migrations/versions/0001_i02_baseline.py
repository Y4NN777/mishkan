"""Create the accepted I02 metadata baseline.

Revision ID: i02_baseline
Revises: none
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i02_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("resume_key", sa.String(64), nullable=False, unique=True),
        sa.Column("repository_id", sa.String(64), nullable=False),
        sa.Column("repository_revision", sa.String(128), nullable=False),
        sa.Column("discovery_fingerprint", sa.String(64), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("outcome_id", sa.String(160), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False, unique=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("task_key", sa.String(64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("contract", sa.Text(), nullable=False),
        sa.UniqueConstraint("run_id", "task_key"),
    )
    op.create_table(
        "accepted_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("task_key", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("run_id", "task_key"),
    )
    op.create_table(
        "task_acceptances",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("task_key", sa.String(64), nullable=False),
        sa.Column(
            "result_id",
            sa.String(36),
            sa.ForeignKey("accepted_results.id"),
            nullable=False,
        ),
        sa.Column("review_payload", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("run_id", "task_key"),
    )
    op.create_table(
        "event_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("aggregate_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(120), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.String(40), nullable=False),
        sa.Column("published_at", sa.String(40), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("event_outbox")
    op.drop_table("task_acceptances")
    op.drop_table("accepted_results")
    op.drop_table("tasks")
    op.drop_table("plans")
    op.drop_table("runs")
