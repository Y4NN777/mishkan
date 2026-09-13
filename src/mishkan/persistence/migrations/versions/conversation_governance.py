"""Add durable conversations, decisions, escalations, and interventions.

Revision ID: conversation_governance_v1
Revises: mission_governance_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "conversation_governance_v1"
down_revision = "mission_governance_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_channels",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("channel_class", sa.String(32), nullable=False),
        sa.Column("mission_id", sa.String(36), sa.ForeignKey("missions.id")),
        sa.Column("branch_id", sa.String(128)),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_conversation_scope", "conversation_channels", ["channel_class", "mission_id"]
    )
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversation_channels.id"),
            nullable=False,
        ),
        sa.Column("author_identity", sa.String(256), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_conversation_messages_order",
        "conversation_messages",
        ["conversation_id", "created_at"],
    )
    op.create_table(
        "mission_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("mission_id", sa.String(36), sa.ForeignKey("missions.id"), nullable=False),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversation_channels.id"),
            nullable=False,
        ),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_index("ix_mission_decisions_order", "mission_decisions", ["mission_id", "created_at"])
    op.create_table(
        "mission_escalations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("mission_id", sa.String(36), sa.ForeignKey("missions.id"), nullable=False),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversation_channels.id"),
            nullable=False,
        ),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_mission_escalations_state",
        "mission_escalations",
        ["mission_id", "state", "updated_at"],
    )
    op.create_table(
        "mission_interventions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("mission_id", sa.String(36), sa.ForeignKey("missions.id"), nullable=False),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversation_channels.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_mission_interventions_order",
        "mission_interventions",
        ["mission_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mission_interventions_order", table_name="mission_interventions")
    op.drop_table("mission_interventions")
    op.drop_index("ix_mission_escalations_state", table_name="mission_escalations")
    op.drop_table("mission_escalations")
    op.drop_index("ix_mission_decisions_order", table_name="mission_decisions")
    op.drop_table("mission_decisions")
    op.drop_index("ix_conversation_messages_order", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_index("ix_conversation_scope", table_name="conversation_channels")
    op.drop_table("conversation_channels")
