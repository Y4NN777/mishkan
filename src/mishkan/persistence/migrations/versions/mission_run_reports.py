"""Add attributable versioned reports for completed multi-task mission runs."""

import sqlalchemy as sa
from alembic import op

revision = "mission_run_reports_v1"
down_revision = "mission_run_bindings_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mission_run_reports",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("mission_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("reporter_identity", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["mission_id"], ["missions.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.UniqueConstraint("mission_id", "run_id", name="uq_mission_run_report"),
    )
    op.create_index(
        "ix_mission_run_reports_mission",
        "mission_run_reports",
        ["mission_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mission_run_reports_mission", table_name="mission_run_reports")
    op.drop_table("mission_run_reports")
