"""Persist normalized bounded workspace evidence for sessions.

Revision ID: session_effect_evidence_v1
Revises: artifact_record_identity_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "session_effect_evidence_v1"
down_revision = "artifact_record_identity_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_sessions",
        sa.Column("effect_evidence_payload", sa.Text(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("execution_sessions", "effect_evidence_payload")
