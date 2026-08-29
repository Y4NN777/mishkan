"""Add immutable skill versions and atomic activation pointers.

Revision ID: skill_lifecycle_v1
Revises: i05_skill_usage_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "skill_lifecycle_v1"
down_revision = "i05_skill_usage_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("skill_name", sa.String(64), nullable=False),
        sa.Column("skill_version", sa.String(128), nullable=False),
        sa.Column("package_fingerprint", sa.String(71), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("skill_name", "skill_version"),
    )
    op.create_index(
        "ix_skill_versions_state",
        "skill_versions",
        ["skill_name", "state", "updated_at"],
    )
    op.create_table(
        "skill_active_versions",
        sa.Column("skill_name", sa.String(64), primary_key=True),
        sa.Column(
            "version_id",
            sa.String(36),
            sa.ForeignKey("skill_versions.id"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "skill_lifecycle_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "version_id",
            sa.String(36),
            sa.ForeignKey("skill_versions.id"),
            nullable=False,
        ),
        sa.Column("disposition", sa.String(32), nullable=False),
        sa.Column("policy_fingerprint", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_skill_decisions_version",
        "skill_lifecycle_decisions",
        ["version_id", "decided_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_skill_decisions_version", table_name="skill_lifecycle_decisions")
    op.drop_table("skill_lifecycle_decisions")
    op.drop_table("skill_active_versions")
    op.drop_index("ix_skill_versions_state", table_name="skill_versions")
    op.drop_table("skill_versions")
