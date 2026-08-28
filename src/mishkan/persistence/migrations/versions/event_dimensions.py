"""Add typed event filter dimensions.

Revision ID: i04_event_dimensions
Revises: i04_runtime_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i04_event_dimensions"
down_revision = "i04_runtime_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("event_outbox") as batch:
        batch.add_column(sa.Column("run_id", sa.String(256), nullable=True))
        batch.add_column(sa.Column("task_id", sa.String(256), nullable=True))
        batch.add_column(sa.Column("identity_id", sa.String(256), nullable=True))
        batch.add_column(sa.Column("team_id", sa.String(256), nullable=True))
        batch.add_column(
            sa.Column("security_relevant", sa.Boolean(), nullable=False, server_default="0")
        )
        batch.create_index("ix_event_outbox_run_cursor", ["run_id", "cursor"])
        batch.create_index("ix_event_outbox_task_cursor", ["task_id", "cursor"])
        batch.create_index("ix_event_outbox_identity_cursor", ["identity_id", "cursor"])
        batch.create_index("ix_event_outbox_team_cursor", ["team_id", "cursor"])
        batch.create_index("ix_event_outbox_security_cursor", ["security_relevant", "cursor"])

    op.execute("UPDATE event_outbox SET run_id = aggregate_id WHERE entity_type = 'run'")
    op.execute(
        "UPDATE event_outbox SET task_id = json_extract(payload, '$.task_id') "
        "WHERE json_type(payload, '$.task_id') = 'text'"
    )
    op.execute(
        "UPDATE event_outbox SET identity_id = "
        "(SELECT actor_id FROM application_commands "
        "WHERE application_commands.command_id = event_outbox.command_id) "
        "WHERE command_id IS NOT NULL"
    )
    op.execute(
        "UPDATE event_outbox SET identity_id = json_extract(payload, '$.identity') "
        "WHERE identity_id IS NULL AND json_type(payload, '$.identity') = 'text'"
    )
    op.execute(
        "UPDATE event_outbox SET team_id = json_extract(payload, '$.team_id') "
        "WHERE json_type(payload, '$.team_id') = 'text'"
    )
    op.execute(
        "UPDATE event_outbox SET security_relevant = 1 "
        "WHERE sensitivity = 'security' OR event_type LIKE 'security.%'"
    )


def downgrade() -> None:
    with op.batch_alter_table("event_outbox") as batch:
        batch.drop_index("ix_event_outbox_security_cursor")
        batch.drop_index("ix_event_outbox_team_cursor")
        batch.drop_index("ix_event_outbox_identity_cursor")
        batch.drop_index("ix_event_outbox_task_cursor")
        batch.drop_index("ix_event_outbox_run_cursor")
        batch.drop_column("security_relevant")
        batch.drop_column("team_id")
        batch.drop_column("identity_id")
        batch.drop_column("task_id")
        batch.drop_column("run_id")
