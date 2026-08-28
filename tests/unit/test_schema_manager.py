from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.persistence import DatabaseState, SchemaManager
from mishkan.persistence.sqlite import Base


def _migration_config(database: Path) -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).parents[2] / "src" / "mishkan" / "persistence" / "migrations"),
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config


def test_empty_database_is_initialized_only_explicitly(tmp_path: Path) -> None:
    database = tmp_path / "mishkan.db"
    manager = SchemaManager(database)

    assert manager.status().state is DatabaseState.EMPTY
    initialized = manager.initialize()

    assert initialized.state is DatabaseState.CURRENT
    with create_engine(f"sqlite:///{database}").connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def test_exact_legacy_database_is_backed_up_and_upgraded(tmp_path: Path) -> None:
    database = tmp_path / "mishkan.db"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for table in (
            "tool_registry_entries",
            "planned_tool_calls",
            "event_retention_plans",
            "event_holds",
            "task_review_rejections",
            "artifact_reconciliation_plans",
            "mcp_progress",
            "mcp_calls",
            "mcp_primitives",
            "mcp_connections",
            "browser_actions",
            "browser_observations",
            "browser_sessions",
            "web_cache_entries",
            "execution_sessions",
            "change_operations",
            "change_sets",
            "artifact_gc_plans",
            "artifact_pins",
            "artifact_holds",
            "artifact_references",
            "artifact_collections",
            "artifact_uploads",
            "artifacts",
        ):
            connection.execute(text(f"DROP TABLE {table}"))
        connection.execute(text("DROP TABLE application_commands"))
        connection.execute(text("DROP TABLE aggregate_revisions"))
        connection.execute(text("ALTER TABLE event_outbox RENAME TO event_outbox_current"))
        connection.execute(
            text(
                """
                CREATE TABLE event_outbox (
                    id VARCHAR(36) PRIMARY KEY,
                    aggregate_id VARCHAR(36) NOT NULL,
                    event_type VARCHAR(120) NOT NULL,
                    payload TEXT NOT NULL,
                    occurred_at VARCHAR(40) NOT NULL,
                    published_at VARCHAR(40)
                )
                """
            )
        )
        connection.execute(text("DROP TABLE event_outbox_current"))
        connection.execute(text("ALTER TABLE runs DROP COLUMN revision"))
        connection.execute(text("ALTER TABLE runs DROP COLUMN cancellation_requested"))
        connection.execute(text("ALTER TABLE runs DROP COLUMN updated_at"))
        connection.execute(text("ALTER TABLE tasks DROP COLUMN revision"))
        connection.execute(text("ALTER TABLE tasks DROP COLUMN attempt_count"))
        connection.execute(text("ALTER TABLE tasks DROP COLUMN updated_at"))
    engine.dispose()

    manager = SchemaManager(database)
    assert manager.status().state is DatabaseState.LEGACY_I02

    upgraded = manager.upgrade()

    assert upgraded.state is DatabaseState.CURRENT
    assert upgraded.backup_path is not None and upgraded.backup_path.is_file()


def test_unknown_database_is_refused_without_mutation(tmp_path: Path) -> None:
    database = tmp_path / "unknown.db"
    with create_engine(f"sqlite:///{database}").begin() as connection:
        connection.execute(text("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)"))

    manager = SchemaManager(database)
    with pytest.raises(MishkanError) as caught:
        manager.upgrade()

    assert caught.value.envelope.code is ErrorCode.VERSION
    assert manager.status().state is DatabaseState.UNKNOWN


def test_registry_record_identity_migration_backfills_existing_entries(tmp_path: Path) -> None:
    database = tmp_path / "registry.db"
    config = _migration_config(database)
    command.upgrade(config, "tool_registry_lifecycle_v1")
    with create_engine(f"sqlite:///{database}").begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tool_registry_entries "
                "(entry_kind, identity, enabled, removed, precedence, revision, updated_at) "
                "VALUES ('adapter', 'native.example', 1, 0, 0, 1, "
                "'2026-08-28T00:00:00+00:00')"
            )
        )

    command.upgrade(config, "head")

    assert SchemaManager(database).status().state is DatabaseState.CURRENT
    with create_engine(f"sqlite:///{database}").connect() as connection:
        record_id = connection.execute(
            text(
                "SELECT record_id FROM tool_registry_entries "
                "WHERE entry_kind = 'adapter' AND identity = 'native.example'"
            )
        ).scalar_one()
    assert str(UUID(record_id)) == record_id
