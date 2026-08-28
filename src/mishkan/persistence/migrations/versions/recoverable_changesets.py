"""Add durable change-set and operation journals.

Revision ID: i03_changesets
Revises: i03_artifacts
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i03_changesets"
down_revision = "i03_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "change_sets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace", sa.Text(), nullable=False),
        sa.Column("scope", sa.String(256), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("operation_index", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("diff_reference", sa.String(64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "change_operations",
        sa.Column("change_set_id", sa.String(36), primary_key=True),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("before_token", sa.Text(), nullable=True),
        sa.Column("preimage_reference", sa.String(64), nullable=True),
        sa.Column("expected_after_token", sa.Text(), nullable=True),
        sa.Column("actual_after_token", sa.Text(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(["change_set_id"], ["change_sets.id"]),
    )
    op.create_index("ix_change_sets_state", "change_sets", ["state", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_change_sets_state", table_name="change_sets")
    op.drop_table("change_operations")
    op.drop_table("change_sets")
