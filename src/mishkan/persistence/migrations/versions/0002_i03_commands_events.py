"""Add durable command receipts, revisions, and globally ordered events.

Revision ID: i03_commands_events
Revises: i02_baseline
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i03_commands_events"
down_revision = "i02_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))

    op.rename_table("event_outbox", "event_outbox_i02")
    op.create_table(
        "event_outbox",
        sa.Column("cursor", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("id", sa.String(36), nullable=False, unique=True),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.Column("aggregate_id", sa.String(256), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(120), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.String(40), nullable=False),
        sa.Column("command_id", sa.String(36), nullable=True),
        sa.Column("correlation_id", sa.String(36), nullable=True),
        sa.Column("causation_id", sa.String(36), nullable=True),
        sa.Column("sensitivity", sa.String(32), nullable=False),
        sa.Column("published_at", sa.String(40), nullable=True),
    )
    op.execute(
        """
        INSERT INTO event_outbox (
            id, schema_version, aggregate_id, entity_type, event_type, source,
            payload, occurred_at, sensitivity, published_at
        )
        SELECT id, '1.0', aggregate_id, 'run', event_type, 'mishkan',
               payload, occurred_at, 'internal', published_at
        FROM event_outbox_i02
        ORDER BY occurred_at, id
        """
    )
    op.drop_table("event_outbox_i02")
    op.create_index("ix_event_outbox_type_cursor", "event_outbox", ["event_type", "cursor"])
    op.create_index(
        "ix_event_outbox_entity_cursor",
        "event_outbox",
        ["entity_type", "aggregate_id", "cursor"],
    )
    op.create_table(
        "application_commands",
        sa.Column("command_id", sa.String(36), primary_key=True),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("command_type", sa.String(128), nullable=False),
        sa.Column("actor_id", sa.String(256), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(256), nullable=True),
        sa.Column("expected_revision", sa.Integer(), nullable=True),
        sa.Column("issued_at", sa.String(40), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result_payload", sa.Text(), nullable=False),
        sa.Column("event_cursor", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "aggregate_revisions",
        sa.Column("entity_type", sa.String(64), primary_key=True),
        sa.Column("entity_id", sa.String(256), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("aggregate_revisions")
    op.drop_table("application_commands")
    op.drop_index("ix_event_outbox_entity_cursor", table_name="event_outbox")
    op.drop_index("ix_event_outbox_type_cursor", table_name="event_outbox")
    op.rename_table("event_outbox", "event_outbox_i03")
    op.create_table(
        "event_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("aggregate_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(120), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.String(40), nullable=False),
        sa.Column("published_at", sa.String(40), nullable=True),
    )
    op.execute(
        """
        INSERT INTO event_outbox (id, aggregate_id, event_type, payload, occurred_at, published_at)
        SELECT id, aggregate_id, event_type, payload, occurred_at, published_at
        FROM event_outbox_i03 ORDER BY cursor
        """
    )
    op.drop_table("event_outbox_i03")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("revision")
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("revision")
