"""Add authoritative artifact manifests, uploads, references, and GC plans.

Revision ID: i03_artifacts
Revises: i03_commands_events
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i03_artifacts"
down_revision = "i03_commands_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("digest", sa.String(71), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(255), nullable=False),
        sa.Column("lifecycle", sa.String(32), nullable=False),
        sa.Column("storage_ref", sa.String(128), nullable=False),
        sa.Column("manifest_payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("tombstoned_at", sa.String(40), nullable=True),
        sa.UniqueConstraint("digest", "size_bytes", "id"),
    )
    op.create_index("ix_artifacts_lifecycle_created", "artifacts", ["lifecycle", "created_at"])
    op.create_table(
        "artifact_uploads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("expected_digest", sa.String(71), nullable=False),
        sa.Column("expected_size", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(255), nullable=False),
        sa.Column("offset", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("artifact_id", sa.String(36), nullable=True),
        sa.Column("staging_path", sa.Text(), nullable=False),
        sa.Column("metadata_payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"]),
    )
    op.create_table(
        "artifact_collections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entries_payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "artifact_references",
        sa.Column("scope", sa.String(256), primary_key=True),
        sa.Column("name", sa.String(256), primary_key=True),
        sa.Column("artifact_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"]),
    )
    op.create_table(
        "artifact_holds",
        sa.Column("artifact_id", sa.String(36), primary_key=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"]),
    )
    op.create_table(
        "artifact_pins",
        sa.Column("artifact_id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"]),
    )
    op.create_table(
        "artifact_gc_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("watermark", sa.String(40), nullable=False),
        sa.Column("candidates_payload", sa.Text(), nullable=False),
        sa.Column("applied_at", sa.String(40), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("artifact_gc_plans")
    op.drop_table("artifact_pins")
    op.drop_table("artifact_holds")
    op.drop_table("artifact_references")
    op.drop_table("artifact_collections")
    op.drop_table("artifact_uploads")
    op.drop_index("ix_artifacts_lifecycle_created", table_name="artifacts")
    op.drop_table("artifacts")
