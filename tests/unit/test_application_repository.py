from __future__ import annotations

from pathlib import Path

import pytest

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
