"""Daemon-owned Unix PTY and managed-job supervision."""

from __future__ import annotations

import base64
import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import psutil  # type: ignore[import-untyped]
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from mishkan.artifacts import ArtifactProvenance
from mishkan.artifacts.service import DurableArtifactService
from mishkan.config.models import SessionConfig, SessionProfileConfig
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.execution.sessions import (
    CursorRead,
    SessionMode,
    SessionRecord,
    SessionRequest,
    SessionState,
)
from mishkan.persistence.migration import SchemaManager
from mishkan.persistence.sqlite import ExecutionSessionRow, LocalRunRepository


class _SecretFilter:
    def __init__(self, secrets: tuple[str, ...]) -> None:
        self._secrets = tuple(secret.encode() for secret in secrets if secret)
        self._tail = b""

    def feed(self, chunk: bytes, *, final: bool = False) -> bytes:
        combined = self._tail + chunk
        for secret in self._secrets:
            combined = combined.replace(secret, b"[REDACTED]")
        if final:
            emit, self._tail = combined, b""
        else:
            keep = max(
                (
                    length
                    for secret in self._secrets
                    for length in range(1, len(secret))
                    if combined.endswith(secret[:length])
                ),
                default=0,
            )
            emit = combined[:-keep] if keep else combined
            self._tail = combined[-keep:] if keep else b""
        return emit


class SessionSupervisor:
    def __init__(
        self,
        database: Path,
        workspace: Path,
        spool_root: Path,
        config: SessionConfig,
        artifacts: DurableArtifactService,
    ) -> None:
        SchemaManager(database).require_current()
        self._workspace = workspace.resolve(strict=True)
        self._spool_root = spool_root.resolve()
        if not self._spool_root.is_relative_to(self._workspace):
            raise MishkanError(ErrorCode.AUTHORITY_NOT_GRANTED, "session spool escapes workspace")
        self._spool_root.mkdir(parents=True, exist_ok=True)
        self._config = config
        self._artifacts = artifacts
        self._engine = create_engine(f"sqlite:///{database.resolve()}")
        event.listen(self._engine, "connect", LocalRunRepository._configure_connection)
        self._processes: dict[UUID, subprocess.Popen[bytes]] = {}
        self._pty_masters: dict[UUID, int] = {}
        self._threads: dict[UUID, tuple[threading.Thread, ...]] = {}
        self._locks: dict[tuple[UUID, str], threading.Lock] = {}

    def start(
        self,
        request: SessionRequest,
        *,
        credential_values: Mapping[str, str] | None = None,
    ) -> SessionRecord:
        profile = self._profile(request.profile)
        workspace = self._session_workspace(request.workspace)
        executable = Path(request.executable)
        if not executable.is_absolute() or not executable.is_file():
            raise MishkanError(
                ErrorCode.EXECUTION, "session executable must be an existing absolute file"
            )
        session_id = uuid4()
        directory = self._spool_root / str(session_id)
        directory.mkdir(mode=0o700)
        stdout_spool = directory / "stdout.spool"
        stderr_spool = directory / "stderr.spool"
        stdout_spool.touch(mode=0o600)
        stderr_spool.touch(mode=0o600)
        resolved = dict(credential_values or {})
        required_locators = {
            reference.locator for reference in request.credential_environment.values()
        }
        if not required_locators.issubset(resolved):
            raise MishkanError(
                ErrorCode.AUTHORIZATION_MISSING,
                "session credential references were not resolved after authorization",
            )
        environment = dict(request.environment)
        environment.update(
            {
                name: resolved[reference.locator]
                for name, reference in request.credential_environment.items()
            }
        )
        secret_values = tuple(resolved.values())
        sanitized_request = request.model_copy(
            update={
                "environment": {name: "[PRESENT]" for name in request.environment},
                "credential_environment": request.credential_environment,
            }
        )
        if request.mode is SessionMode.PTY:
            process, threads = self._start_pty(
                session_id,
                request,
                workspace,
                environment,
                stdout_spool,
                profile,
                secret_values,
            )
        else:
            process, threads = self._start_job(
                session_id,
                request,
                workspace,
                environment,
                stdout_spool,
                stderr_spool,
                profile,
                secret_values,
            )
        identity = psutil.Process(process.pid)
        now = utc_now()
        with Session(self._engine) as session, session.begin():
            session.add(
                ExecutionSessionRow(
                    id=str(session_id),
                    mode=request.mode.value,
                    state=SessionState.RUNNING.value,
                    owner=request.owner,
                    run_id=request.run_id,
                    task_id=request.task_id,
                    workspace=request.workspace,
                    profile=request.profile,
                    request_payload=sanitized_request.model_dump_json(),
                    pid=process.pid,
                    process_group_id=os.getpgid(process.pid),
                    process_create_time=identity.create_time(),
                    stdout_spool=str(stdout_spool.relative_to(self._spool_root)),
                    stderr_spool=str(stderr_spool.relative_to(self._spool_root)),
                    stdout_cursor=0,
                    stderr_cursor=0,
                    exit_code=None,
                    signal=None,
                    stdout_artifact_reference=None,
                    stderr_artifact_reference=None,
                    cancellation_requested=False,
                    deadline=request.deadline.isoformat(),
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )
        self._processes[session_id] = process
        self._threads[session_id] = threads
        if request.mode is SessionMode.JOB and request.readiness is not None:
            self._await_readiness(session_id, request, profile)
        return self.status(session_id)

    def write(self, session_id: UUID, content: bytes) -> int:
        descriptor = self._pty_masters.get(session_id)
        if descriptor is None:
            raise MishkanError(ErrorCode.EXECUTION, "PTY master is unavailable")
        self._require_live_identity(session_id)
        return os.write(descriptor, content)

    def resize(self, session_id: UUID, *, rows: int, columns: int) -> None:
        descriptor = self._pty_masters.get(session_id)
        if descriptor is None:
            raise MishkanError(ErrorCode.EXECUTION, "PTY master is unavailable")
        fcntl.ioctl(descriptor, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    def read(
        self,
        session_id: UUID,
        *,
        channel: Literal["stdout", "stderr"],
        offset: int,
        limit: int,
        binary: bool = False,
    ) -> CursorRead:
        if channel not in {"stdout", "stderr"} or offset < 0 or limit < 1 or limit > 16_777_216:
            raise MishkanError(ErrorCode.OUTPUT_CONTRACT, "session cursor request is invalid")
        row = self._row(session_id)
        path = self._spool_path(row, channel)
        size = path.stat().st_size
        if offset > size:
            raise MishkanError(
                ErrorCode.REVISION_MISMATCH,
                "session cursor is beyond the durable spool",
                details={"cursor": offset, "size": size},
            )
        with path.open("rb") as stream:
            stream.seek(offset)
            content = stream.read(limit)
        encoding: Literal["utf-8", "base64"] = "base64" if binary else "utf-8"
        data = base64.b64encode(content).decode() if binary else content.decode(errors="replace")
        return CursorRead(
            session_id=session_id,
            channel=channel,
            offset=offset,
            next_offset=offset + len(content),
            encoding=encoding,
            data=data,
            eof=offset + len(content) >= size
            and row.state
            in {
                SessionState.SETTLED.value,
                SessionState.FAILED.value,
                SessionState.LOST.value,
                SessionState.UNCERTAIN.value,
            },
        )

    def signal(self, session_id: UUID, signal_name: str) -> SessionRecord:
        row = self._require_live_identity(session_id)
        profile = self._profile(row.profile)
        allowed = set(profile.cancellation_signals)
        if signal_name not in allowed:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED, "signal is not allowed by session profile"
            )
        signum = self._signal_number(signal_name)
        assert row.process_group_id is not None
        os.killpg(row.process_group_id, signum)
        return self._record(self._row(session_id))

    def cancel(self, session_id: UUID) -> SessionRecord:
        with Session(self._engine) as session, session.begin():
            row = session.get(ExecutionSessionRow, str(session_id))
            if row is None:
                raise MishkanError(ErrorCode.EXECUTION, "session does not exist")
            row.cancellation_requested = True
            row.state = SessionState.CANCELLING.value
            row.updated_at = utc_now().isoformat()
            profile_name = row.profile
        profile = self._profile(profile_name)
        for signal_name in profile.cancellation_signals:
            try:
                self.signal(session_id, signal_name)
            except MishkanError as error:
                if error.envelope.code is ErrorCode.RUN_INTERRUPTED:
                    break
                raise
            deadline = time.monotonic() + profile.grace_seconds
            while time.monotonic() < deadline:
                if not self._identity_matches(self._row(session_id)):
                    return self.settle(session_id)
                time.sleep(min(0.05, profile.grace_seconds))
        return self.settle(session_id)

    def status(self, session_id: UUID) -> SessionRecord:
        row = self._row(session_id)
        process = self._processes.get(session_id)
        if process is not None:
            returncode = process.poll()
            if (
                returncode is None
                and not row.cancellation_requested
                and utc_now() >= datetime.fromisoformat(row.deadline)
            ):
                return self.cancel(session_id)
            if returncode is not None and row.state not in {
                SessionState.SETTLED.value,
                SessionState.FAILED.value,
            }:
                return self.settle(session_id)
        elif row.state in {SessionState.RUNNING.value, SessionState.READY.value}:
            if row.mode == SessionMode.PTY.value:
                self._update_state(session_id, SessionState.LOST)
            elif not self._identity_matches(row):
                self._update_state(session_id, SessionState.UNCERTAIN)
        return self._record(self._row(session_id))

    def settle(self, session_id: UUID) -> SessionRecord:
        row = self._row(session_id)
        process = self._processes.get(session_id)
        if process is not None and process.poll() is None:
            return self._record(row)
        descriptor = self._pty_masters.pop(session_id, None)
        if descriptor is not None:
            os.close(descriptor)
        for thread in self._threads.get(session_id, ()):
            thread.join(timeout=self._profile(row.profile).settle_timeout_seconds)
        exit_code = process.returncode if process is not None else row.exit_code
        stdout = self._spool_path(row, "stdout").read_bytes()
        stderr = self._spool_path(row, "stderr").read_bytes()
        stdout_reference = self._output_artifact(row, "stdout", stdout)
        stderr_reference = self._output_artifact(row, "stderr", stderr)
        with Session(self._engine) as session, session.begin():
            current = session.get(ExecutionSessionRow, str(session_id))
            assert current is not None
            current.state = (
                SessionState.SETTLED.value if exit_code == 0 else SessionState.FAILED.value
            )
            current.exit_code = exit_code
            current.signal = -exit_code if exit_code is not None and exit_code < 0 else None
            current.stdout_cursor = len(stdout)
            current.stderr_cursor = len(stderr)
            current.stdout_artifact_reference = stdout_reference
            current.stderr_artifact_reference = stderr_reference
            current.updated_at = utc_now().isoformat()
        return self._record(self._row(session_id))

    def reconcile_all(self) -> tuple[SessionRecord, ...]:
        with Session(self._engine) as session:
            identifiers = session.scalars(
                select(ExecutionSessionRow.id).where(
                    ExecutionSessionRow.state.in_(
                        (
                            SessionState.RUNNING.value,
                            SessionState.READY.value,
                            SessionState.CANCELLING.value,
                        )
                    )
                )
            ).all()
        return tuple(self.status(UUID(identifier)) for identifier in identifiers)

    def list(self, *, offset: int = 0, limit: int = 100) -> tuple[SessionRecord, ...]:
        if offset < 0 or limit < 1 or limit > 1_000:
            raise MishkanError(ErrorCode.OUTPUT_CONTRACT, "session query bound is invalid")
        with Session(self._engine) as session:
            rows = session.scalars(
                select(ExecutionSessionRow)
                .order_by(ExecutionSessionRow.created_at, ExecutionSessionRow.id)
                .offset(offset)
                .limit(limit)
            ).all()
            return tuple(self._record(row) for row in rows)

    def _start_pty(
        self,
        session_id: UUID,
        request: SessionRequest,
        workspace: Path,
        environment: dict[str, str],
        spool: Path,
        profile: SessionProfileConfig,
        secrets: tuple[str, ...],
    ) -> tuple[subprocess.Popen[bytes], tuple[threading.Thread, ...]]:
        master, slave = pty.openpty()
        fcntl.ioctl(
            slave, termios.TIOCSWINSZ, struct.pack("HHHH", request.rows, request.columns, 0, 0)
        )
        process = subprocess.Popen(
            [request.executable, *request.arguments],
            cwd=workspace,
            env=environment,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)
        self._pty_masters[session_id] = master
        thread = self._reader_thread(
            session_id,
            "stdout",
            master,
            spool,
            secrets,
            profile,
            close_descriptor=False,
        )
        return process, (thread,)

    def _start_job(
        self,
        session_id: UUID,
        request: SessionRequest,
        workspace: Path,
        environment: dict[str, str],
        stdout_spool: Path,
        stderr_spool: Path,
        profile: SessionProfileConfig,
        secrets: tuple[str, ...],
    ) -> tuple[subprocess.Popen[bytes], tuple[threading.Thread, ...]]:
        process = subprocess.Popen(
            [request.executable, *request.arguments],
            cwd=workspace,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
        assert process.stdout is not None and process.stderr is not None
        stdout_descriptor = os.dup(process.stdout.fileno())
        stderr_descriptor = os.dup(process.stderr.fileno())
        process.stdout.close()
        process.stderr.close()
        stdout = self._reader_thread(
            session_id,
            "stdout",
            stdout_descriptor,
            stdout_spool,
            secrets,
            profile,
        )
        stderr = self._reader_thread(
            session_id,
            "stderr",
            stderr_descriptor,
            stderr_spool,
            secrets,
            profile,
        )
        return process, (stdout, stderr)

    def _reader_thread(
        self,
        session_id: UUID,
        channel: str,
        descriptor: int,
        spool: Path,
        secrets: tuple[str, ...],
        profile: SessionProfileConfig,
        *,
        close_descriptor: bool = True,
    ) -> threading.Thread:
        lock = self._locks.setdefault((session_id, channel), threading.Lock())

        def reader() -> None:
            filtered = _SecretFilter(secrets)
            written = 0
            try:
                while written < profile.max_output_bytes:
                    try:
                        chunk = os.read(
                            descriptor,
                            min(profile.read_chunk_bytes, profile.max_output_bytes - written),
                        )
                    except OSError:
                        break
                    if not chunk:
                        break
                    clean = filtered.feed(chunk)
                    with lock, spool.open("ab", buffering=0) as stream:
                        stream.write(clean)
                    written += len(clean)
                tail = filtered.feed(b"", final=True)
                if tail:
                    with lock, spool.open("ab", buffering=0) as stream:
                        stream.write(tail)
            finally:
                if close_descriptor:
                    with suppress(OSError):
                        os.close(descriptor)

        thread = threading.Thread(
            target=reader, name=f"mishkan-{session_id}-{channel}", daemon=True
        )
        thread.start()
        return thread

    def _await_readiness(
        self,
        session_id: UUID,
        request: SessionRequest,
        profile: SessionProfileConfig,
    ) -> None:
        assert request.readiness is not None
        deadline = time.monotonic() + request.readiness.timeout_seconds
        while time.monotonic() < deadline:
            process = self._processes[session_id]
            if process.poll() is not None:
                self.settle(session_id)
                return
            ready = request.readiness.kind == "process_running"
            if request.readiness.kind == "output_contains":
                value = (request.readiness.value or "").encode()
                row = self._row(session_id)
                ready = value in self._spool_path(row, "stdout").read_bytes()
            if ready:
                self._update_state(session_id, SessionState.READY)
                return
            time.sleep(profile.readiness_poll_seconds)
        self.cancel(session_id)

    def _require_live_identity(self, session_id: UUID) -> ExecutionSessionRow:
        row = self._row(session_id)
        if not self._identity_matches(row):
            raise MishkanError(
                ErrorCode.RUN_INTERRUPTED,
                "process identity cannot be proven; no signal was sent",
            )
        return row

    @staticmethod
    def _identity_matches(row: ExecutionSessionRow) -> bool:
        if row.pid is None or row.process_create_time is None or row.process_group_id is None:
            return False
        try:
            process = psutil.Process(row.pid)
            return (
                process.is_running()
                and abs(process.create_time() - row.process_create_time) < 0.000_001
                and os.getpgid(row.pid) == row.process_group_id
            )
        except (psutil.Error, OSError):
            return False

    def _row(self, session_id: UUID) -> ExecutionSessionRow:
        with Session(self._engine) as session:
            row = session.get(ExecutionSessionRow, str(session_id))
            if row is None:
                raise MishkanError(ErrorCode.EXECUTION, "session does not exist")
            session.expunge(row)
            return row

    def _record(self, row: ExecutionSessionRow) -> SessionRecord:
        return SessionRecord(
            session_id=UUID(row.id),
            mode=SessionMode(row.mode),
            state=SessionState(row.state),
            owner=row.owner,
            run_id=row.run_id,
            task_id=row.task_id,
            pid=row.pid,
            process_group_id=row.process_group_id,
            process_create_time=row.process_create_time,
            stdout_cursor=self._spool_path(row, "stdout").stat().st_size,
            stderr_cursor=self._spool_path(row, "stderr").stat().st_size,
            exit_code=row.exit_code,
            signal=row.signal,
            stdout_artifact_reference=row.stdout_artifact_reference,
            stderr_artifact_reference=row.stderr_artifact_reference,
            cancellation_requested=row.cancellation_requested,
            created_at=datetime.fromisoformat(row.created_at),
            updated_at=datetime.fromisoformat(row.updated_at),
        )

    def _spool_path(self, row: ExecutionSessionRow, channel: str) -> Path:
        relative = row.stdout_spool if channel == "stdout" else row.stderr_spool
        path = (self._spool_root / relative).resolve()
        if not path.is_relative_to(self._spool_root):
            raise MishkanError(ErrorCode.EXECUTION, "session spool path escaped its root")
        return path

    def _session_workspace(self, relative: str) -> Path:
        path = (self._workspace / relative).resolve(strict=True)
        if not path.is_relative_to(self._workspace):
            raise MishkanError(ErrorCode.AUTHORITY_NOT_GRANTED, "session workspace escaped project")
        return path

    def _profile(self, name: str) -> SessionProfileConfig:
        profile = self._config.profiles.get(name)
        if profile is None:
            raise MishkanError(ErrorCode.CONFIGURATION, "session profile does not exist")
        return profile

    @staticmethod
    def _signal_number(name: str) -> int:
        try:
            return int(getattr(signal, f"SIG{name}"))
        except (AttributeError, ValueError) as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION, "session profile signal is unsupported"
            ) from exc

    def _update_state(self, session_id: UUID, state: SessionState) -> None:
        with Session(self._engine) as session, session.begin():
            row = session.get(ExecutionSessionRow, str(session_id))
            assert row is not None
            row.state = state.value
            row.updated_at = utc_now().isoformat()

    def _output_artifact(self, row: ExecutionSessionRow, channel: str, content: bytes) -> str:
        return self._artifacts.put_bytes(
            content,
            media_type="application/octet-stream",
            provenance=ArtifactProvenance(
                producer_identity=row.owner,
                run_id=row.run_id,
                task_attempt_id=row.task_id,
                call_id=row.id,
                capability=f"session.{row.mode}",
                channel=channel,
            ),
            complete=True,
            retention="session",
        ).reference
