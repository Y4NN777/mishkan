"""Add immutable professional evidence and promotion decisions.

Revision ID: professional_evolution_v1
Revises: mission_environment_plans_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "professional_evolution_v1"
down_revision = "mission_environment_plans_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "professional_evidence",
        sa.Column("evidence_id", sa.String(36), primary_key=True),
        sa.Column("identity_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("scope_level", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("fresh_until", sa.String(40), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_professional_evidence_subject",
        "professional_evidence",
        ["identity_id", "kind", "subject", "observed_at"],
    )
    op.create_table(
        "professional_promotions",
        sa.Column("decision_id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), nullable=False, unique=True),
        sa.Column("identity_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("disposition", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("identity_id", "kind", "subject", "revision"),
    )
    op.create_index(
        "ix_professional_promotions_subject",
        "professional_promotions",
        ["identity_id", "kind", "subject", "revision"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_professional_promotions_subject",
        table_name="professional_promotions",
    )
    op.drop_table("professional_promotions")
    op.drop_index(
        "ix_professional_evidence_subject",
        table_name="professional_evidence",
    )
    op.drop_table("professional_evidence")
