"""Add explicit event-retention plans and holds.

Revision ID: i04_event_retention
Revises: i04_event_dimensions
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i04_event_retention"
down_revision = "i04_event_dimensions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_holds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(256), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.String(256), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("released_at", sa.String(40), nullable=True),
    )
    op.create_index(
        "ix_event_holds_active_scope", "event_holds", ["released_at", "scope", "scope_id"]
    )
    op.create_table(
        "event_retention_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("policy_payload", sa.Text(), nullable=False),
        sa.Column("policy_fingerprint", sa.String(64), nullable=False),
        sa.Column("cutoff", sa.String(40), nullable=False),
        sa.Column("candidates_payload", sa.Text(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("deleted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("applied_at", sa.String(40), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("event_retention_plans")
    op.drop_index("ix_event_holds_active_scope", table_name="event_holds")
    op.drop_table("event_holds")
