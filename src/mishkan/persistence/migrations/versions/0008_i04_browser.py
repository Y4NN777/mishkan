"""Add durable Browser sessions, observations, and actions.

Revision ID: i04_browser
Revises: i04_web
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i04_browser"
down_revision = "i04_web"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_identity", sa.String(256), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("task_attempt_id", sa.String(128), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_browser_session_owner_state",
        "browser_sessions",
        ["owner_identity", "state"],
    )
    op.create_table(
        "browser_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("browser_sessions.id"),
            nullable=False,
        ),
        sa.Column("page_id", sa.String(128), nullable=False),
        sa.Column("session_revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("expires_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_browser_observation_session_page",
        "browser_observations",
        ["session_id", "page_id", "created_at"],
    )
    op.create_table(
        "browser_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(36), nullable=False, unique=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("browser_sessions.id"),
            nullable=False,
        ),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("completed_at", sa.String(40), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("browser_actions")
    op.drop_index("ix_browser_observation_session_page", table_name="browser_observations")
    op.drop_table("browser_observations")
    op.drop_index("ix_browser_session_owner_state", table_name="browser_sessions")
    op.drop_table("browser_sessions")
