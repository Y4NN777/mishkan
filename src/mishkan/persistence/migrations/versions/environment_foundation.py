"""Add environment observations, bindings, descriptors, attempts, and verification.

Revision ID: i05_environment_v1
Revises: i05_skill_learning_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i05_environment_v1"
down_revision = "i05_skill_learning_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "environment_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("context_id", sa.String(256), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_environment_observation_context",
        "environment_observations",
        ["context_id", "observed_at"],
    )
    op.create_table(
        "environment_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), unique=True, nullable=False),
        sa.Column("observation_id", sa.String(36), nullable=False),
        sa.Column("context_id", sa.String(256), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("resolved_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_environment_binding_context",
        "environment_bindings",
        ["context_id", "resolved_at"],
    )
    for table_name, id_name in (
        ("environment_descriptor_sets", "descriptor_set_id"),
        ("environment_attempts", "attempt_id"),
        ("environment_verifications", "verification_id"),
    ):
        op.create_table(
            table_name,
            sa.Column(id_name, sa.String(36), primary_key=True),
            sa.Column("binding_id", sa.String(36), nullable=False),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.Column("recorded_at", sa.String(40), nullable=False),
        )
        op.create_index(
            f"ix_{table_name}_binding",
            table_name,
            ["binding_id", "recorded_at"],
        )


def downgrade() -> None:
    for table_name in (
        "environment_verifications",
        "environment_attempts",
        "environment_descriptor_sets",
    ):
        op.drop_index(f"ix_{table_name}_binding", table_name=table_name)
        op.drop_table(table_name)
    op.drop_index("ix_environment_binding_context", table_name="environment_bindings")
    op.drop_table("environment_bindings")
    op.drop_index(
        "ix_environment_observation_context",
        table_name="environment_observations",
    )
    op.drop_table("environment_observations")
