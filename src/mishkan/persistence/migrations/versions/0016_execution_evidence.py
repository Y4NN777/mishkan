"""Persist complete execution settlement evidence.

Revision ID: execution_evidence_v1
Revises: i03_change_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "execution_evidence_v1"
down_revision = "i03_change_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_sessions",
        sa.Column("before_state_payload", sa.Text(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "execution_sessions",
        sa.Column("observed_effects_payload", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "execution_sessions",
        sa.Column("produced_artifacts_payload", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column("execution_sessions", sa.Column("effect_settlement", sa.String(32)))
    op.add_column("execution_sessions", sa.Column("termination_cause", sa.String(64)))
    op.add_column(
        "execution_sessions",
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("execution_sessions", sa.Column("error", sa.Text()))
    op.add_column(
        "execution_sessions",
        sa.Column(
            "started_at", sa.String(40), nullable=False, server_default="1970-01-01T00:00:00+00:00"
        ),
    )
    op.add_column("execution_sessions", sa.Column("finished_at", sa.String(40)))


def downgrade() -> None:
    for name in (
        "finished_at",
        "started_at",
        "error",
        "retryable",
        "termination_cause",
        "effect_settlement",
        "produced_artifacts_payload",
        "observed_effects_payload",
        "before_state_payload",
    ):
        op.drop_column("execution_sessions", name)
