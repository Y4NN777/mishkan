from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.persistence import DatabaseState, SchemaManager


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
    command.upgrade(_migration_config(database), "i02_baseline")
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
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


def test_legacy_lookalike_with_wrong_column_contract_is_refused(tmp_path: Path) -> None:
    database = tmp_path / "lookalike.db"
    command.upgrade(_migration_config(database), "i02_baseline")
    with create_engine(f"sqlite:///{database}").begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
        connection.execute(text("ALTER TABLE event_outbox RENAME TO original_events"))
        connection.execute(
            text(
                """
                CREATE TABLE event_outbox (
                    id VARCHAR(36) PRIMARY KEY,
                    aggregate_id VARCHAR(36) NOT NULL,
                    event_type VARCHAR(120) NOT NULL,
                    payload VARCHAR(999) NOT NULL,
                    occurred_at VARCHAR(40) NOT NULL,
                    published_at VARCHAR(40)
                )
                """
            )
        )
        connection.execute(text("DROP TABLE original_events"))

    manager = SchemaManager(database)
    assert manager.status().state is DatabaseState.UNKNOWN
    with pytest.raises(MishkanError) as caught:
        manager.upgrade()
    assert caught.value.envelope.code is ErrorCode.VERSION


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


def test_artifact_auxiliary_record_identity_migration_backfills_existing_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "artifacts.db"
    config = _migration_config(database)
    command.upgrade(config, "tool_registry_record_identity_v1")
    artifact_id = "11111111-1111-4111-8111-111111111111"
    with create_engine(f"sqlite:///{database}").begin() as connection:
        connection.execute(
            text(
                "INSERT INTO artifact_references "
                "(scope, name, artifact_id, revision, prior_artifact_id, prior_revision, "
                "updated_at) "
                "VALUES ('run:test', 'latest', :artifact_id, 1, NULL, NULL, :timestamp)"
            ),
            {"artifact_id": artifact_id, "timestamp": "2026-08-28T00:00:00+00:00"},
        )
        connection.execute(
            text(
                "INSERT INTO artifact_holds (artifact_id, reason, created_at) "
                "VALUES (:artifact_id, 'test hold', :timestamp)"
            ),
            {"artifact_id": artifact_id, "timestamp": "2026-08-28T00:00:00+00:00"},
        )
        connection.execute(
            text(
                "INSERT INTO artifact_pins (artifact_id, created_at) "
                "VALUES (:artifact_id, :timestamp)"
            ),
            {"artifact_id": artifact_id, "timestamp": "2026-08-28T00:00:00+00:00"},
        )

    command.upgrade(config, "head")

    with create_engine(f"sqlite:///{database}").connect() as connection:
        identities = [
            connection.execute(text(f"SELECT record_id FROM {table}")).scalar_one()
            for table in ("artifact_references", "artifact_holds", "artifact_pins")
        ]
    assert len(set(identities)) == 3
    assert all(str(UUID(identity)) == identity for identity in identities)


def test_session_effect_evidence_migration_backfills_existing_sessions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sessions.db"
    config = _migration_config(database)
    command.upgrade(config, "artifact_record_identity_v1")
    timestamp = "2026-08-28T00:00:00+00:00"
    with create_engine(f"sqlite:///{database}").begin() as connection:
        connection.execute(
            text(
                "INSERT INTO execution_sessions "
                "(id, mode, state, owner, run_id, task_id, workspace, profile, "
                "request_payload, stdout_spool, stderr_spool, stdout_cursor, stderr_cursor, "
                "before_state_payload, observed_effects_payload, produced_artifacts_payload, "
                "retryable, cancellation_requested, deadline, started_at, created_at, updated_at) "
                "VALUES (:id, 'job', 'settled', 'engineer', :run_id, 'task-1', '.', "
                "'standard', '{}', 'stdout', 'stderr', 0, 0, '{}', '[]', '[]', 0, 0, "
                ":timestamp, :timestamp, :timestamp, :timestamp)"
            ),
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "run_id": "22222222-2222-4222-8222-222222222222",
                "timestamp": timestamp,
            },
        )

    command.upgrade(config, "head")

    with create_engine(f"sqlite:///{database}").connect() as connection:
        evidence = connection.execute(
            text("SELECT effect_evidence_payload FROM execution_sessions")
        ).scalar_one()
        column = next(
            row
            for row in connection.execute(text("PRAGMA table_info(execution_sessions)"))
            if row.name == "effect_evidence_payload"
        )
    assert evidence == "{}"
    assert column.notnull == 1
