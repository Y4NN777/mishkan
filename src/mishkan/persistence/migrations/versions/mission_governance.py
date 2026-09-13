"""Add durable organization rosters, missions, Briefs, and contextual crews.

Revision ID: mission_governance_v1
Revises: i05_environment_verify_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "mission_governance_v1"
down_revision = "i05_environment_verify_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organization_rosters",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("organization_version", sa.String(64), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "missions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("organization_version", sa.String(64), nullable=False),
        sa.Column("current_brief_version", sa.Integer()),
        sa.Column("current_crew_version", sa.Integer()),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_index("ix_missions_state_updated", "missions", ["state", "updated_at"])
    op.create_table(
        "mission_briefs",
        sa.Column("brief_id", sa.String(36), primary_key=True),
        sa.Column("mission_id", sa.String(36), sa.ForeignKey("missions.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("mission_id", "version"),
    )
    op.create_index(
        "ix_mission_briefs_mission_created",
        "mission_briefs",
        ["mission_id", "created_at"],
    )
    op.create_table(
        "mission_crews",
        sa.Column("crew_id", sa.String(36), primary_key=True),
        sa.Column("mission_id", sa.String(36), sa.ForeignKey("missions.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("brief_version", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("mission_id", "version"),
    )
    op.create_index(
        "ix_mission_crews_mission_created",
        "mission_crews",
        ["mission_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mission_crews_mission_created", table_name="mission_crews")
    op.drop_table("mission_crews")
    op.drop_index("ix_mission_briefs_mission_created", table_name="mission_briefs")
    op.drop_table("mission_briefs")
    op.drop_index("ix_missions_state_updated", table_name="missions")
    op.drop_table("missions")
    op.drop_table("organization_rosters")
