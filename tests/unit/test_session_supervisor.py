from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text

from mishkan.artifacts.service import DurableArtifactService
from mishkan.config.loader import ConfigLoader
from mishkan.config.models import (
    CredentialReference,
    CredentialSource,
    ProjectConfig,
    SessionEffectObservationConfig,
)
from mishkan.config.presets import preset_text
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.execution import (
    ExecutionMode,
    ExecutionRequest,
    ExecutionSession,
    ReadinessProbe,
    SessionState,
    SessionSupervisor,
)
from mishkan.persistence import SchemaManager


def _supervisor(
    tmp_path: Path,
    *,
    profile_updates: dict[str, object] | None = None,
) -> tuple[SessionSupervisor, DurableArtifactService, Path]:
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
            **(profile_updates or {}),
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


def _request(mode: ExecutionMode, arguments: tuple[str, ...]) -> ExecutionRequest:
    return ExecutionRequest(
        mode=mode,
        owner="engineer",
        run_id="run-1",
        task_id="task-1",
        executable="/bin/sh",
        args=arguments,
        session_profile="standard",
        deadline=utc_now() + timedelta(minutes=1),
        policy_fingerprint="a" * 64,
    )


def _await_settlement(supervisor: SessionSupervisor, session_id: UUID) -> ExecutionSession:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = supervisor.status(session_id)
        if record.state in {
            SessionState.SETTLED,
            SessionState.FAILED,
            SessionState.LOST,
            SessionState.UNCERTAIN,
        }:
            return record
        time.sleep(0.02)
    raise AssertionError("session did not settle")


def test_job_readiness_cursors_artifacts_and_cross_chunk_secret_filter(tmp_path: Path) -> None:
    supervisor, artifacts, _database = _supervisor(tmp_path)
    request = _request(
        ExecutionMode.JOB,
        (
            "-c",
            "printf CAN; sleep 0.05; printf 'ARY READY\\n'; sleep 0.5",
        ),
    ).model_copy(
        update={
            "credential_environment": {
                "TOKEN": CredentialReference(source=CredentialSource.ENV, locator="TEST_TOKEN")
            },
            "credential_references": (),
            "readiness": ReadinessProbe(kind="output_contains", value="READY"),
        }
    )
    started = supervisor.start(request, credential_values={"TEST_TOKEN": "CANARY"})
    assert started.state is SessionState.READY
    settled = _await_settlement(supervisor, started.session_id)
    assert settled.state is SessionState.SETTLED
    output = supervisor.read(started.session_id, channel="stdout", offset=0, limit=1024)
    assert "CANARY" not in output.data
    assert "[REDACTED]" in output.data
    reference = settled.stdout_artifact_reference
    assert reference is not None
    assert b"CANARY" not in artifacts.read_bytes(reference)


def test_stateful_pty_input_is_bounded_and_settles_with_observed_artifacts(tmp_path: Path) -> None:
    supervisor, _artifacts, _database = _supervisor(tmp_path)
    started = supervisor.start(
        _request(ExecutionMode.PTY, ()).model_copy(update={"declared_paths": ("governed-effect",)})
    )
    assert started.result is None

    with pytest.raises(MishkanError) as oversized:
        supervisor.write(started.session_id, b"x" * 1_048_577)
    assert oversized.value.envelope.code is ErrorCode.OUTPUT_CONTRACT

    supervisor.write(
        started.session_id,
        b"touch governed-effect; printf done; exit\n",
        declared_effects=("filesystem.write",),
    )
    settled = _await_settlement(supervisor, started.session_id)

    assert (tmp_path / "governed-effect").is_file()
    assert settled.result is not None
    assert settled.declared_effects == ("filesystem.write",)
    assert settled.effect_settlement == "completed"
    assert settled.result.changed_paths == ("governed-effect",)
    assert settled.result.scope_deviations == ()
    assert settled.result.effect_observation_complete is True
    assert settled.result.effect_diff_artifact_ref is not None
    assert settled.observed_effects == ("filesystem.change:governed-effect",)
    assert len(settled.result.produced_artifact_refs) == 1
    assert settled.result.started_at < settled.result.finished_at
    rooted = _artifacts.plan_gc(watermark=utc_now() + timedelta(seconds=1))
    assert settled.stdout_artifact_reference not in rooted.candidates
    assert settled.result.produced_artifact_refs[0] not in rooted.candidates
    assert settled.result.effect_diff_artifact_ref not in rooted.candidates
    assert settled.retryable is False
    assert "done" in settled.stdout_preview
    assert settled.execution_location == "local"


def test_pty_input_resize_cursor_and_lost_on_new_supervisor(tmp_path: Path) -> None:
    supervisor, artifacts, database = _supervisor(tmp_path)
    request = _request(ExecutionMode.PTY, ())
    started = supervisor.start(request)
    supervisor.resize(started.session_id, rows=40, columns=120)
    with pytest.raises(MishkanError):
        supervisor.resize(started.session_id, rows=0, columns=120)
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
    lost = replacement.status(started.session_id)
    assert lost.state is SessionState.LOST
    assert lost.result is not None and lost.result.status == "lost"
    assert lost.stdout_artifact_reference is not None
    supervisor.cancel(started.session_id)


def test_cancellation_and_recycled_pid_identity_fail_closed(tmp_path: Path) -> None:
    supervisor, _artifacts, database = _supervisor(tmp_path)
    running = supervisor.start(_request(ExecutionMode.JOB, ("-c", "sleep 10")))
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
    assert cancelled.state is SessionState.SETTLED
    assert cancelled.result is not None and cancelled.result.status == "cancelled"


def test_unattended_session_monitor_enforces_deadline_without_status_polling(
    tmp_path: Path,
) -> None:
    supervisor, _artifacts, _database = _supervisor(tmp_path)
    request = _request(ExecutionMode.JOB, ("-c", "sleep 10")).model_copy(
        update={"deadline": utc_now() + timedelta(milliseconds=150)}
    )

    started = supervisor.start(request)
    time.sleep(0.8)
    settled = supervisor.status(started.session_id)

    assert settled.state is SessionState.SETTLED
    assert settled.result is not None
    assert settled.result.status == "timed_out"
    assert settled.result.termination_cause == "timed_out"


def test_unattended_session_monitor_enforces_sanitized_output_bound(tmp_path: Path) -> None:
    supervisor, _artifacts, _database = _supervisor(
        tmp_path,
        profile_updates={"max_output_bytes": 64},
    )
    started = supervisor.start(
        _request(
            ExecutionMode.JOB,
            ("-c", "printf '%04096d' 0; sleep 10"),
        )
    )
    time.sleep(0.8)
    settled = supervisor.status(started.session_id)

    assert settled.state is SessionState.SETTLED
    assert settled.result is not None
    assert settled.result.termination_cause == "output_limit"
    assert settled.result.truncated is True
    assert settled.result.stdout_bytes <= 64


def test_recovered_job_monitor_enforces_persisted_deadline_after_daemon_restart(
    tmp_path: Path,
) -> None:
    supervisor, artifacts, database = _supervisor(tmp_path)
    supervisor._ensure_monitor = lambda _session_id: None  # type: ignore[assignment]
    started = supervisor.start(
        _request(ExecutionMode.JOB, ("-c", "sleep 10")).model_copy(
            update={"deadline": utc_now() + timedelta(milliseconds=150)}
        )
    )
    config = ConfigLoader().load([tmp_path / "config.yaml"]).value
    assert config.sessions is not None
    replacement = SessionSupervisor(
        database,
        tmp_path,
        tmp_path / config.sessions.spool_root,
        config.sessions,
        artifacts,
    )

    recovered = replacement.reconcile_all()
    assert recovered[0].state is SessionState.RUNNING
    time.sleep(0.8)
    settled = replacement.status(started.session_id)

    assert settled.state is SessionState.SETTLED
    assert settled.result is not None and settled.result.status == "timed_out"


def test_session_scope_deviation_is_durable_uncertainty(tmp_path: Path) -> None:
    supervisor, _artifacts, _database = _supervisor(tmp_path)
    started = supervisor.start(
        _request(ExecutionMode.PTY, ()).model_copy(update={"declared_paths": ("allowed",)})
    )

    supervisor.write(
        started.session_id,
        b"touch outside; exit\n",
        declared_effects=("filesystem.write",),
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        settled = supervisor.status(started.session_id)
        if settled.state is SessionState.UNCERTAIN:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("scope-deviating session did not settle as uncertain")

    assert settled.result is not None
    assert settled.result.status == "uncertain"
    assert settled.result.changed_paths == ("outside",)
    assert settled.result.scope_deviations == ("outside",)
    assert settled.result.effect_settlement == "uncertain"


def test_incomplete_workspace_observation_cannot_be_accepted(tmp_path: Path) -> None:
    (tmp_path / "existing-a").write_text("a")
    (tmp_path / "existing-b").write_text("b")
    supervisor, _artifacts, _database = _supervisor(
        tmp_path,
        profile_updates={
            "effect_observation": SessionEffectObservationConfig(
                max_entries=1,
                max_file_bytes=1024,
                max_total_bytes=1024,
                exclude=(".git/**", ".mishkan/**"),
            )
        },
    )
    started = supervisor.start(_request(ExecutionMode.JOB, ("-c", "true")))
    settled = _await_settlement(supervisor, started.session_id)

    assert settled.state is SessionState.UNCERTAIN
    assert settled.result is not None
    assert settled.result.status == "uncertain"
    assert settled.result.effect_observation_complete is False
    assert "entry_limit_reached" in settled.result.effect_observation_omissions
