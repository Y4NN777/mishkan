"""Give every persisted tool-registry record a stable UUID.

Revision ID: tool_registry_record_identity_v1
Revises: tool_registry_lifecycle_v1
"""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "tool_registry_record_identity_v1"
down_revision = "tool_registry_lifecycle_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tool_registry_entries",
        sa.Column("record_id", sa.String(36), nullable=True),
    )
    connection = op.get_bind()
    entries = connection.execute(
        sa.text("SELECT entry_kind, identity FROM tool_registry_entries")
    ).all()
    for entry_kind, identity in entries:
        connection.execute(
            sa.text(
                "UPDATE tool_registry_entries SET record_id = :record_id "
                "WHERE entry_kind = :entry_kind AND identity = :identity"
            ),
            {
                "record_id": str(uuid4()),
                "entry_kind": entry_kind,
                "identity": identity,
            },
        )
    with op.batch_alter_table("tool_registry_entries") as batch:
        batch.alter_column("record_id", existing_type=sa.String(36), nullable=False)
        batch.create_unique_constraint("uq_tool_registry_entry_record_id", ["record_id"])


def downgrade() -> None:
    with op.batch_alter_table("tool_registry_entries") as batch:
        batch.drop_constraint("uq_tool_registry_entry_record_id", type_="unique")
        batch.drop_column("record_id")
