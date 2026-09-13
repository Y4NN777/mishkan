"""Bind missions to exact repository or prospective-workspace runs."""

import sqlalchemy as sa
from alembic import op

revision = "mission_run_bindings_v1"
down_revision = "run_execution_contexts_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mission_run_bindings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("mission_id", sa.String(length=36), nullable=False),
        sa.Column("binding_key", sa.String(length=128), nullable=False),
        sa.Column("binding_revision", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("acceptance", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["mission_id"], ["missions.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.UniqueConstraint(
            "mission_id",
            "binding_key",
            "binding_revision",
            name="uq_mission_run_binding_revision",
        ),
    )
    op.create_index(
        "ix_mission_run_bindings_mission",
        "mission_run_bindings",
        ["mission_id", "binding_key", "binding_revision"],
    )


def downgrade() -> None:
    op.drop_index("ix_mission_run_bindings_mission", table_name="mission_run_bindings")
    op.drop_table("mission_run_bindings")
