"""Add durable PTY and managed-job session records.

Revision ID: i03_sessions
Revises: i03_changesets
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i03_sessions"
down_revision = "i03_changesets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("owner", sa.String(256), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(256), nullable=False),
        sa.Column("workspace", sa.Text(), nullable=False),
        sa.Column("profile", sa.String(128), nullable=False),
        sa.Column("request_payload", sa.Text(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("process_group_id", sa.Integer(), nullable=True),
        sa.Column("process_create_time", sa.Float(), nullable=True),
        sa.Column("stdout_spool", sa.Text(), nullable=False),
        sa.Column("stderr_spool", sa.Text(), nullable=False),
        sa.Column("stdout_cursor", sa.Integer(), nullable=False),
        sa.Column("stderr_cursor", sa.Integer(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("signal", sa.Integer(), nullable=True),
        sa.Column("stdout_artifact_reference", sa.String(64), nullable=True),
        sa.Column("stderr_artifact_reference", sa.String(64), nullable=True),
        sa.Column("cancellation_requested", sa.Boolean(), nullable=False),
        sa.Column("deadline", sa.String(40), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_index("ix_execution_sessions_state", "execution_sessions", ["state", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_execution_sessions_state", table_name="execution_sessions")
    op.drop_table("execution_sessions")
