from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from mishkan.artifacts.service import DurableArtifactService
from mishkan.config.loader import ConfigLoader
from mishkan.config.models import ProjectConfig
from mishkan.config.presets import preset_text
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.execution import (
    ReadinessProbe,
    SessionMode,
    SessionRequest,
    SessionState,
    SessionSupervisor,
)
from mishkan.persistence import SchemaManager


def _supervisor(tmp_path: Path) -> tuple[SessionSupervisor, DurableArtifactService, Path]:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(preset_text("local"))
    config = (
        ConfigLoader()
        .load([config_path])
        .value.model_copy(update={"project": ProjectConfig(workspace=tmp_path)})
    )
    assert config.sessions is not None
    profile = config.sessions.profiles["standard"].model_copy(
        update={
            "read_chunk_bytes": 2,
            "readiness_poll_seconds": 0.02,
            "grace_seconds": 0.05,
        }
    )
    sessions = config.sessions.model_copy(update={"profiles": {"standard": profile}})
    database = tmp_path / ".mishkan" / "mishkan.db"
    SchemaManager(database).initialize()
    artifacts = DurableArtifactService(
        database,
        tmp_path / ".mishkan" / "artifacts",
        max_artifact_bytes=1024 * 1024,
        max_chunk_bytes=1024,
    )
    supervisor = SessionSupervisor(
        database,
        tmp_path,
        tmp_path / sessions.spool_root,
        sessions,
        artifacts,
    )
    return supervisor, artifacts, database


def _request(mode: SessionMode, arguments: tuple[str, ...]) -> SessionRequest:
    return SessionRequest(
        mode=mode,
        owner="engineer",
        run_id="run-1",
        task_id="task-1",
        executable="/bin/sh",
        arguments=arguments,
        profile="standard",
        deadline=utc_now() + timedelta(minutes=1),
        policy_fingerprint="a" * 64,
    )


def _await_settlement(supervisor: SessionSupervisor, session_id) -> object:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = supervisor.status(session_id)
        if record.state in {SessionState.SETTLED, SessionState.FAILED}:
            return record
        time.sleep(0.02)
    raise AssertionError("session did not settle")


def test_job_readiness_cursors_artifacts_and_cross_chunk_secret_filter(tmp_path: Path) -> None:
    supervisor, artifacts, _database = _supervisor(tmp_path)
    request = _request(
        SessionMode.JOB,
        ("-c", "printf CAN; sleep 0.05; printf 'ARY READY\\n'; sleep 0.2"),
    ).model_copy(
        update={
            "credential_environment": {"TOKEN": "CANARY"},
            "credential_references": ("env:TOKEN",),
            "readiness": ReadinessProbe(kind="output_contains", value="READY"),
        }
    )
    started = supervisor.start(request)
    assert started.state is SessionState.READY
    settled = _await_settlement(supervisor, started.session_id)
    assert settled.state is SessionState.SETTLED  # type: ignore[attr-defined]
    output = supervisor.read(started.session_id, channel="stdout", offset=0, limit=1024)
    assert "CANARY" not in output.data
    assert "[REDACTED]" in output.data
    reference = settled.stdout_artifact_reference  # type: ignore[attr-defined]
    assert reference is not None
    assert b"CANARY" not in artifacts.read_bytes(reference)


def test_pty_input_resize_cursor_and_lost_on_new_supervisor(tmp_path: Path) -> None:
    supervisor, artifacts, database = _supervisor(tmp_path)
    request = _request(SessionMode.PTY, ())
    started = supervisor.start(request)
    supervisor.resize(started.session_id, rows=40, columns=120)
    supervisor.write(started.session_id, b"printf 'hello\\n'\n")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        output = supervisor.read(started.session_id, channel="stdout", offset=0, limit=1024)
        if "hello" in output.data:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("PTY output was not observed")

    config_path = tmp_path / "config.yaml"
    config = ConfigLoader().load([config_path]).value
    assert config.sessions is not None
    replacement = SessionSupervisor(
        database,
        tmp_path,
        tmp_path / config.sessions.spool_root,
        config.sessions,
        artifacts,
    )
    assert replacement.status(started.session_id).state is SessionState.LOST
    supervisor.cancel(started.session_id)


def test_cancellation_and_recycled_pid_identity_fail_closed(tmp_path: Path) -> None:
    supervisor, _artifacts, database = _supervisor(tmp_path)
    running = supervisor.start(_request(SessionMode.JOB, ("-c", "sleep 10")))
    with create_engine(f"sqlite:///{database}").begin() as connection:
        connection.execute(
            text(
                "UPDATE execution_sessions SET process_create_time = process_create_time + 1 "
                "WHERE id = :id"
            ),
            {"id": str(running.session_id)},
        )
    with pytest.raises(MishkanError) as caught:
        supervisor.signal(running.session_id, "TERM")
    assert caught.value.envelope.code is ErrorCode.RUN_INTERRUPTED

    with create_engine(f"sqlite:///{database}").begin() as connection:
        connection.execute(
            text(
                "UPDATE execution_sessions SET process_create_time = process_create_time - 1 "
                "WHERE id = :id"
            ),
            {"id": str(running.session_id)},
        )
    cancelled = supervisor.cancel(running.session_id)
    assert cancelled.cancellation_requested
    assert cancelled.state in {SessionState.FAILED, SessionState.SETTLED}
