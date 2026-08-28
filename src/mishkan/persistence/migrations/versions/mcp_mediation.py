"""Add durable MCP connections, primitives, calls, and progress.

Revision ID: i04_mcp
Revises: i04_browser
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i04_mcp"
down_revision = "i04_browser"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("connection_id", sa.String(128), nullable=False, unique=True),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("transport", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("schema_fingerprint", sa.String(64), nullable=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_mcp_connection_direction_state",
        "mcp_connections",
        ["direction", "state"],
    )
    op.create_table(
        "mcp_primitives",
        sa.Column("connection_id", sa.String(128), primary_key=True),
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("name", sa.String(256), primary_key=True),
        sa.Column("schema_hash", sa.String(71), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("discovered_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "mcp_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(36), nullable=False, unique=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("connection_id", sa.String(128), nullable=False),
        sa.Column("primitive_name", sa.String(256), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("request_payload", sa.Text(), nullable=False),
        sa.Column("result_payload", sa.Text(), nullable=True),
        sa.Column("remote_task_id", sa.String(256), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_mcp_call_connection_state",
        "mcp_calls",
        ["connection_id", "state", "updated_at"],
    )
    op.create_table(
        "mcp_progress",
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("cursor", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("mcp_progress")
    op.drop_index("ix_mcp_call_connection_state", table_name="mcp_calls")
    op.drop_table("mcp_calls")
    op.drop_table("mcp_primitives")
    op.drop_index("ix_mcp_connection_direction_state", table_name="mcp_connections")
    op.drop_table("mcp_connections")
