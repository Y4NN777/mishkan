"""Enforce organization-wide conversation and decision invariants.

Revision ID: organization_concurrency_v1
Revises: mission_run_reports_v1
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "organization_concurrency_v1"
down_revision = "mission_run_reports_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mission_decisions",
        sa.Column("supersedes_decision_id", sa.String(36), nullable=True),
    )
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, payload FROM mission_decisions")).mappings()
    for row in rows:
        supersedes = json.loads(row["payload"]).get("supersedes_decision_id")
        if supersedes is not None:
            connection.execute(
                sa.text(
                    "UPDATE mission_decisions "
                    "SET supersedes_decision_id = :supersedes WHERE id = :decision_id"
                ),
                {"supersedes": supersedes, "decision_id": row["id"]},
            )
    op.create_index(
        "uq_conversation_single_executive",
        "conversation_channels",
        ["channel_class"],
        unique=True,
        sqlite_where=sa.text("channel_class = 'executive'"),
        postgresql_where=sa.text("channel_class = 'executive'"),
    )
    op.create_index(
        "uq_mission_decision_settlement",
        "mission_decisions",
        ["supersedes_decision_id"],
        unique=True,
        sqlite_where=sa.text("supersedes_decision_id IS NOT NULL"),
        postgresql_where=sa.text("supersedes_decision_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_mission_decision_settlement", table_name="mission_decisions")
    op.drop_index("uq_conversation_single_executive", table_name="conversation_channels")
    op.drop_column("mission_decisions", "supersedes_decision_id")
