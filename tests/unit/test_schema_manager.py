from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.persistence import DatabaseState, SchemaManager
from mishkan.persistence.sqlite import Base


def test_empty_database_is_initialized_only_explicitly(tmp_path: Path) -> None:
    database = tmp_path / "mishkan.db"
    manager = SchemaManager(database)

    assert manager.status().state is DatabaseState.EMPTY
    initialized = manager.initialize()

    assert initialized.state is DatabaseState.CURRENT
    with create_engine(f"sqlite:///{database}").connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def test_exact_i02_database_is_backed_up_and_upgraded(tmp_path: Path) -> None:
    database = tmp_path / "mishkan.db"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for table in (
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
        connection.execute(text("ALTER TABLE event_outbox RENAME TO event_outbox_i03"))
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
        connection.execute(text("DROP TABLE event_outbox_i03"))
        connection.execute(text("ALTER TABLE runs DROP COLUMN revision"))
        connection.execute(text("ALTER TABLE tasks DROP COLUMN revision"))
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
