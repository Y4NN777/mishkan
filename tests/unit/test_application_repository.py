from __future__ import annotations

from pathlib import Path
from time import perf_counter

import pytest
from sqlalchemy import create_engine, text

from mishkan.application import ApplicationCommand, CommandStatus
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.persistence import SchemaManager, SQLiteApplicationRepository


def _repository(tmp_path: Path) -> SQLiteApplicationRepository:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    return SQLiteApplicationRepository(database)


def test_command_event_and_revision_are_committed_atomically(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    command = ApplicationCommand(
        command_type="run.cancel",
        actor_id="local-operator",
        target_type="run",
        target_id="run-1",
        expected_revision=0,
        payload={"reason": "operator request"},
    )

    result = repository.accept(
        command,
        target_id="run-1",
        event_type="run.cancelled",
        result_payload={"cancelled": True},
        event_payload={"reason": "operator request"},
    )

    assert result.status is CommandStatus.ACCEPTED
    assert result.revision == 1
    assert result.event_cursor == 1
    assert repository.command_result(str(command.command_id)) == result
    page = repository.events()
    assert page.next_cursor == 1
    assert page.events[0].command_id == command.command_id
    assert page.events[0].payload == {"reason": "operator request"}


def test_exact_command_retry_returns_original_result(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    command = ApplicationCommand(
        command_type="artifact.hold.set",
        actor_id="local-operator",
        target_type="artifact",
        target_id="artifact-1",
        payload={},
    )
    first = repository.accept(
        command,
        target_id="artifact-1",
        event_type="artifact.hold_set",
    )
    retried = repository.accept(
        command,
        target_id="artifact-1",
        event_type="artifact.hold_set",
    )

    assert retried == first
    assert len(repository.events().events) == 1


def test_reused_command_identity_with_different_content_is_refused(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    command = ApplicationCommand(
        command_type="run.cancel",
        actor_id="local-operator",
        target_type="run",
        target_id="run-1",
        payload={"reason": "first"},
    )
    repository.accept(command, target_id="run-1", event_type="run.cancelled")

    with pytest.raises(MishkanError) as caught:
        repository.accept(
            command.model_copy(update={"payload": {"reason": "different"}}),
            target_id="run-1",
            event_type="run.cancelled",
        )

    assert caught.value.envelope.code is ErrorCode.DUPLICATE_RESULT


def test_stale_expected_revision_is_refused_without_event(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = ApplicationCommand(
        command_type="run.pause",
        actor_id="local-operator",
        target_type="run",
        target_id="run-1",
        expected_revision=0,
        payload={},
    )
    repository.accept(first, target_id="run-1", event_type="run.paused")
    stale = first.model_copy(update={"command_id": None, "command_type": "run.resume"})
    stale = ApplicationCommand.model_validate(
        {**stale.model_dump(mode="json", exclude={"command_id"}), "expected_revision": 0}
    )

    with pytest.raises(MishkanError) as caught:
        repository.accept(stale, target_id="run-1", event_type="run.resumed")

    assert caught.value.envelope.code is ErrorCode.REVISION_MISMATCH
    assert len(repository.events().events) == 1


def test_interrupted_reserved_command_is_never_reexecuted_implicitly(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    command = ApplicationCommand(
        command_type="session.start",
        actor_id="local-operator",
        target_type="session_service",
        target_id="local-instance",
        payload={"effect": "started-before-crash"},
    )

    assert repository.reserve(command, target_id="local-instance") is None
    replay = repository.reserve(command, target_id="local-instance")

    assert replay is not None
    assert replay.status is CommandStatus.REFUSED
    assert replay.error is not None
    assert replay.error.details == {
        "command_id": str(command.command_id),
        "reconciliation_required": True,
        "automatic_retry": False,
    }
    assert repository.events().events == ()


def test_reserved_completion_is_atomic(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    command = ApplicationCommand(
        command_type="system.checkpoint",
        actor_id="local-operator",
        target_type="checkpoint",
        target_id="reserved",
        expected_revision=0,
        payload={"index": 0},
    )

    assert repository.reserve(command, target_id="reserved") is None
    result = repository.complete_reserved(
        command,
        target_id="reserved",
        event_type="system.checkpoint_recorded",
    )

    assert result.status is CommandStatus.ACCEPTED
    assert result.event_cursor == 1
    assert tuple(event.cursor for event in repository.events().events) == (1,)


def test_event_ingestion_exceeds_gate(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    total = 150
    started = perf_counter()
    for index in range(total):
        command = ApplicationCommand(
            command_type="system.checkpoint",
            actor_id="local-operator",
            target_type="checkpoint",
            target_id=str(index),
            expected_revision=0,
            payload={"index": index},
        )
        result = repository.accept(
            command,
            target_id=str(index),
            event_type="system.checkpoint_recorded",
        )
        assert result.status is CommandStatus.ACCEPTED
    elapsed = perf_counter() - started

    assert total / elapsed >= 100
    page = repository.events(limit=total)
    assert tuple(event.cursor for event in page.events) == tuple(range(1, total + 1))


def test_reused_reserved_identity_with_changed_content_conflicts(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    command = ApplicationCommand(
        command_type="session.start",
        actor_id="local-operator",
        target_type="session_service",
        payload={"mode": "job"},
    )
    repository.reserve(command, target_id="local-instance")

    with pytest.raises(MishkanError) as caught:
        repository.reserve(
            command.model_copy(update={"payload": {"mode": "pty"}}),
            target_id="local-instance",
        )

    assert caught.value.envelope.code is ErrorCode.DUPLICATE_RESULT


def test_removed_event_cursor_requires_a_fresh_snapshot(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    database = tmp_path / "mishkan.db"
    for index in range(3):
        command = ApplicationCommand(
            command_type="system.checkpoint",
            actor_id="local-operator",
            target_type="system",
            target_id="local-instance",
            payload={"index": index},
        )
        repository.accept(
            command,
            target_id="local-instance",
            event_type="system.checkpoint_recorded",
        )
    with create_engine(f"sqlite:///{database}").begin() as connection:
        connection.execute(text("DELETE FROM event_outbox WHERE cursor <= 2"))

    with pytest.raises(MishkanError) as gap:
        repository.events(after_cursor=1)
    assert gap.value.envelope.code is ErrorCode.RUN_INTERRUPTED
    assert gap.value.envelope.details["category"] == "cursor_gap"
    assert gap.value.envelope.details["snapshot_required"] is True

    with pytest.raises(MishkanError) as unbounded:
        repository.events(limit=0)
    assert unbounded.value.envelope.code is ErrorCode.OUTPUT_CONTRACT
