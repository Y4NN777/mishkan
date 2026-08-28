"""Add the policy-governed tool registry lifecycle projection.

Revision ID: tool_registry_lifecycle_v1
Revises: planned_call_journal_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "tool_registry_lifecycle_v1"
down_revision = "planned_call_journal_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_registry_entries",
        sa.Column("entry_kind", sa.String(16), primary_key=True),
        sa.Column("identity", sa.String(128), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("removed", sa.Boolean(), nullable=False),
        sa.Column("precedence", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("definition_payload", sa.Text()),
        sa.Column("definition_fingerprint", sa.String(64)),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("tool_registry_entries")
