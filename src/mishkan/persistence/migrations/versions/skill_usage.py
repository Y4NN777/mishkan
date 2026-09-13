"""Add durable skill hit, partial, and miss evidence.

Revision ID: i05_skill_usage_v1
Revises: event_cursor_highwater_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i05_skill_usage_v1"
down_revision = "event_cursor_highwater_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_usage",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(256), nullable=False),
        sa.Column("task_class", sa.String(256), nullable=False),
        sa.Column("consuming_identity", sa.String(256), nullable=False),
        sa.Column("requested_skill", sa.String(64), nullable=False),
        sa.Column("skill_version", sa.String(128)),
        sa.Column("package_fingerprint", sa.String(71)),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("policy_fingerprint", sa.String(64), nullable=False),
        sa.Column("evidence_payload", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_skill_usage_task_outcome",
        "skill_usage",
        ["task_class", "outcome", "recorded_at"],
    )
    op.create_index(
        "ix_skill_usage_identity",
        "skill_usage",
        ["requested_skill", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_skill_usage_identity", table_name="skill_usage")
    op.drop_index("ix_skill_usage_task_outcome", table_name="skill_usage")
    op.drop_table("skill_usage")
