"""Add explicit repository and prospective-workspace run contexts.

Revision ID: run_execution_contexts_v1
Revises: professional_evolution_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "run_execution_contexts_v1"
down_revision = "professional_evolution_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("context_kind", sa.String(32), nullable=True))
    op.add_column("runs", sa.Column("context_id", sa.String(256), nullable=True))
    op.add_column("runs", sa.Column("context_revision", sa.String(128), nullable=True))
    op.execute(
        "UPDATE runs SET context_kind = 'repository', "
        "context_id = repository_id, context_revision = repository_revision"
    )
    with op.batch_alter_table("runs") as batch:
        batch.alter_column("context_kind", existing_type=sa.String(32), nullable=False)
        batch.alter_column("context_id", existing_type=sa.String(256), nullable=False)
        batch.alter_column("context_revision", existing_type=sa.String(128), nullable=False)
        batch.alter_column("repository_id", existing_type=sa.String(64), nullable=True)
        batch.alter_column("repository_revision", existing_type=sa.String(128), nullable=True)

    op.create_table(
        "repository_establishments",
        sa.Column("establishment_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("prospective_workspace_id", sa.String(256), nullable=False),
        sa.Column("discovery_revision", sa.String(128), nullable=False),
        sa.Column("repository_id", sa.String(64), nullable=False),
        sa.Column("repository_revision", sa.String(128), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("established_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("run_id"),
    )


def downgrade() -> None:
    op.drop_table("repository_establishments")
    with op.batch_alter_table("runs") as batch:
        batch.alter_column("repository_id", existing_type=sa.String(64), nullable=False)
        batch.alter_column("repository_revision", existing_type=sa.String(128), nullable=False)
        batch.drop_column("context_revision")
        batch.drop_column("context_id")
        batch.drop_column("context_kind")
