"""Add durable skill-learning request and proposal lineage.

Revision ID: i05_skill_learning_v1
Revises: skill_lifecycle_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i05_skill_learning_v1"
down_revision = "skill_lifecycle_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_learning",
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(256), nullable=False),
        sa.Column("task_class", sa.String(256), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_skill_learning_task_state",
        "skill_learning",
        ["task_class", "state", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_skill_learning_task_state", table_name="skill_learning")
    op.drop_table("skill_learning")
