"""Give persisted artifact references, holds, and pins stable UUIDs.

Revision ID: artifact_record_identity_v1
Revises: tool_registry_record_identity_v1
"""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "artifact_record_identity_v1"
down_revision = "tool_registry_record_identity_v1"
branch_labels = None
depends_on = None


def _add_identity(table: str, key_columns: tuple[str, ...]) -> None:
    op.add_column(table, sa.Column("record_id", sa.String(36), nullable=True))
    connection = op.get_bind()
    keys = connection.execute(sa.text(f"SELECT {', '.join(key_columns)} FROM {table}")).all()
    predicate = " AND ".join(f"{column} = :{column}" for column in key_columns)
    statement = sa.text(f"UPDATE {table} SET record_id = :record_id WHERE {predicate}")
    for values in keys:
        parameters = dict(zip(key_columns, values, strict=True))
        parameters["record_id"] = str(uuid4())
        connection.execute(statement, parameters)
    with op.batch_alter_table(table) as batch:
        batch.alter_column("record_id", existing_type=sa.String(36), nullable=False)
        batch.create_unique_constraint(f"uq_{table}_record_id", ["record_id"])


def upgrade() -> None:
    _add_identity("artifact_references", ("scope", "name"))
    _add_identity("artifact_holds", ("artifact_id",))
    _add_identity("artifact_pins", ("artifact_id",))


def downgrade() -> None:
    for table in ("artifact_pins", "artifact_holds", "artifact_references"):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"uq_{table}_record_id", type_="unique")
            batch.drop_column("record_id")
