"""Add accountable task assignments and mission transition history.

Revision ID: mission_assignments_v1
Revises: conversation_governance_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "mission_assignments_v1"
down_revision = "conversation_governance_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mission_assignments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("mission_id", sa.String(36), sa.ForeignKey("missions.id"), nullable=False),
        sa.Column("task_id", sa.String(256), nullable=False),
        sa.Column("assignment_revision", sa.Integer(), nullable=False),
        sa.Column("accountable_owner", sa.String(128), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("mission_id", "task_id", "assignment_revision"),
    )
    op.create_index(
        "ix_mission_assignments_owner",
        "mission_assignments",
        ["mission_id", "accountable_owner", "created_at"],
    )
    op.create_table(
        "mission_transitions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("mission_id", sa.String(36), sa.ForeignKey("missions.id"), nullable=False),
        sa.Column("from_state", sa.String(32), nullable=False),
        sa.Column("to_state", sa.String(32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_mission_transitions_order",
        "mission_transitions",
        ["mission_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mission_transitions_order", table_name="mission_transitions")
    op.drop_table("mission_transitions")
    op.drop_index("ix_mission_assignments_owner", table_name="mission_assignments")
    op.drop_table("mission_assignments")
