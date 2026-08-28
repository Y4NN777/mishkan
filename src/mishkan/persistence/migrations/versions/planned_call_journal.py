"""Persist exact planned capability calls and their terminal results.

Revision ID: planned_call_journal_v1
Revises: execution_evidence_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "planned_call_journal_v1"
down_revision = "execution_evidence_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "planned_tool_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("task_attempt_id", sa.String(128), nullable=False),
        sa.Column("planned_call_id", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("tool_id", sa.String(128), nullable=False),
        sa.Column("tool_version", sa.String(64), nullable=False),
        sa.Column("effect_class", sa.String(64), nullable=False),
        sa.Column("declared_effects_payload", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("effect_settlement", sa.String(32)),
        sa.Column("result_payload", sa.Text()),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.UniqueConstraint(
            "run_id",
            "task_attempt_id",
            "planned_call_id",
            name="uq_planned_tool_call_identity",
        ),
    )
    op.create_index("ix_planned_tool_calls_run", "planned_tool_calls", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_planned_tool_calls_run", table_name="planned_tool_calls")
    op.drop_table("planned_tool_calls")
