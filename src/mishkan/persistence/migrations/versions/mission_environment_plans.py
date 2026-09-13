"""Add accepted agent-authored mission environment plans.

Revision ID: mission_environment_plans_v1
Revises: mission_assignments_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "mission_environment_plans_v1"
down_revision = "mission_assignments_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "missions",
        sa.Column("current_environment_plan_version", sa.Integer()),
    )
    op.create_table(
        "mission_environment_plans",
        sa.Column("plan_id", sa.String(36), primary_key=True),
        sa.Column("mission_id", sa.String(36), sa.ForeignKey("missions.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("owner_identity", sa.String(128), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("mission_id", "version"),
    )
    op.create_index(
        "ix_mission_environment_plans_owner",
        "mission_environment_plans",
        ["mission_id", "owner_identity", "accepted_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mission_environment_plans_owner",
        table_name="mission_environment_plans",
    )
    op.drop_table("mission_environment_plans")
    op.drop_column("missions", "current_environment_plan_version")
