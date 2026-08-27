"""SQLAlchemy repository with SQLite/WAL and transactional outbox semantics."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.identity import new_id
from mishkan.domain.time import utc_now
from mishkan.planning.models import AcceptedPlan, InitializationResult, PlanTask, ReviewDecision
from mishkan.repository.models import DiscoverySnapshot
from mishkan.runtime import RunState, TaskState
from mishkan.tools.gateway_models import AuditEvent


class Base(DeclarativeBase):
    pass


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    resume_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    repository_id: Mapped[str] = mapped_column(String(64), nullable=False)
    repository_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    discovery_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    outcome_id: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)
    plan: Mapped[PlanRow | None] = relationship(back_populates="run", uselist=False)


class PlanRow(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), unique=True, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_at: Mapped[str] = mapped_column(String(40), nullable=False)
    run: Mapped[RunRow] = relationship(back_populates="plan")


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("run_id", "task_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    task_key: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contract: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class ResultRow(Base):
    __tablename__ = "accepted_results"
    __table_args__ = (UniqueConstraint("run_id", "task_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    task_key: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_at: Mapped[str] = mapped_column(String(40), nullable=False)


class AcceptanceRow(Base):
    __tablename__ = "task_acceptances"
    __table_args__ = (UniqueConstraint("run_id", "task_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    task_key: Mapped[str] = mapped_column(String(64), nullable=False)
    result_id: Mapped[str] = mapped_column(ForeignKey("accepted_results.id"), nullable=False)
    review_payload: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_at: Mapped[str] = mapped_column(String(40), nullable=False)


class OutboxRow(Base):
    __tablename__ = "event_outbox"

    cursor: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    aggregate_id: Mapped[str] = mapped_column(String(256), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[str] = mapped_column(String(40), nullable=False)
    command_id: Mapped[str | None] = mapped_column(String(36))
    correlation_id: Mapped[str | None] = mapped_column(String(36))
    causation_id: Mapped[str | None] = mapped_column(String(36))
    sensitivity: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")
    published_at: Mapped[str | None] = mapped_column(String(40))


class CommandRow(Base):
    __tablename__ = "application_commands"

    command_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    command_type: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(256), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(256))
    expected_revision: Mapped[int | None] = mapped_column(Integer)
    issued_at: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_payload: Mapped[str] = mapped_column(Text, nullable=False)
    event_cursor: Mapped[int | None] = mapped_column(Integer)
    completed_at: Mapped[str] = mapped_column(String(40), nullable=False)


class AggregateRevisionRow(Base):
    __tablename__ = "aggregate_revisions"

    entity_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    digest: Mapped[str] = mapped_column(String(71), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest_payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    tombstoned_at: Mapped[str | None] = mapped_column(String(40))


class ArtifactUploadRow(Base):
    __tablename__ = "artifact_uploads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    expected_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    expected_size: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    offset: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    staging_path: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class ArtifactCollectionRow(Base):
    __tablename__ = "artifact_collections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    entries_payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class ArtifactReferenceRow(Base):
    __tablename__ = "artifact_references"

    scope: Mapped[str] = mapped_column(String(256), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class ArtifactHoldRow(Base):
    __tablename__ = "artifact_holds"

    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), primary_key=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class ArtifactPinRow(Base):
    __tablename__ = "artifact_pins"

    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class ArtifactGCPlanRow(Base):
    __tablename__ = "artifact_gc_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    watermark: Mapped[str] = mapped_column(String(40), nullable=False)
    candidates_payload: Mapped[str] = mapped_column(Text, nullable=False)
    applied_at: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class ChangeSetRow(Base):
    __tablename__ = "change_sets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(String(256), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_index: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    diff_reference: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class WebCacheRow(Base):
    __tablename__ = "web_cache_entries"

    key: Mapped[str] = mapped_column(String(71), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    stored_at: Mapped[str] = mapped_column(String(40), nullable=False)
    fresh_until: Mapped[str] = mapped_column(String(40), nullable=False)


class BrowserSessionRow(Base):
    __tablename__ = "browser_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    task_attempt_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class BrowserObservationRow(Base):
    __tablename__ = "browser_observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("browser_sessions.id"), nullable=False)
    page_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    expires_at: Mapped[str] = mapped_column(String(40), nullable=False)


class BrowserActionRow(Base):
    __tablename__ = "browser_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    session_id: Mapped[str] = mapped_column(ForeignKey("browser_sessions.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    completed_at: Mapped[str | None] = mapped_column(String(40))


class McpConnectionRow(Base):
    __tablename__ = "mcp_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_fingerprint: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class McpPrimitiveRow(Base):
    __tablename__ = "mcp_primitives"

    connection_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), primary_key=True)
    schema_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    discovered_at: Mapped[str] = mapped_column(String(40), nullable=False)


class McpCallRow(Base):
    __tablename__ = "mcp_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    connection_id: Mapped[str] = mapped_column(String(128), nullable=False)
    primitive_name: Mapped[str] = mapped_column(String(256), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    request_payload: Mapped[str] = mapped_column(Text, nullable=False)
    result_payload: Mapped[str | None] = mapped_column(Text)
    remote_task_id: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class McpProgressRow(Base):
    __tablename__ = "mcp_progress"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cursor: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class ChangeOperationRow(Base):
    __tablename__ = "change_operations"

    change_set_id: Mapped[str] = mapped_column(ForeignKey("change_sets.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    before_token: Mapped[str | None] = mapped_column(Text)
    preimage_reference: Mapped[str | None] = mapped_column(String(64))
    expected_after_token: Mapped[str | None] = mapped_column(Text)
    actual_after_token: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class ExecutionSessionRow(Base):
    __tablename__ = "execution_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    owner: Mapped[str] = mapped_column(String(256), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    task_id: Mapped[str] = mapped_column(String(256), nullable=False)
    workspace: Mapped[str] = mapped_column(Text, nullable=False)
    profile: Mapped[str] = mapped_column(String(128), nullable=False)
    request_payload: Mapped[str] = mapped_column(Text, nullable=False)
    pid: Mapped[int | None] = mapped_column(Integer)
    process_group_id: Mapped[int | None] = mapped_column(Integer)
    process_create_time: Mapped[float | None] = mapped_column(Float)
    stdout_spool: Mapped[str] = mapped_column(Text, nullable=False)
    stderr_spool: Mapped[str] = mapped_column(Text, nullable=False)
    stdout_cursor: Mapped[int] = mapped_column(Integer, nullable=False)
    stderr_cursor: Mapped[int] = mapped_column(Integer, nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    signal: Mapped[int | None] = mapped_column(Integer)
    stdout_artifact_reference: Mapped[str | None] = mapped_column(String(64))
    stderr_artifact_reference: Mapped[str | None] = mapped_column(String(64))
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, nullable=False)
    deadline: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    run_id: str
    resumed: bool
    plan: AcceptedPlan | None
    results: tuple[InitializationResult, ...]
    reviews: tuple[ReviewDecision, ...]

    @property
    def completed_task_ids(self) -> frozenset[str]:
        return frozenset(result.task_id for result in self.results)


class LocalRunRepository:
    def __init__(self, database_path: Path) -> None:
        from mishkan.persistence.migration import SchemaManager

        SchemaManager(database_path).require_current()
        self._engine = create_engine(f"sqlite:///{database_path}")
        event.listen(self._engine, "connect", self._configure_connection)

    @staticmethod
    def _configure_connection(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    def start_or_resume(
        self,
        discovery: DiscoverySnapshot,
        objective: str,
        outcome_id: str,
    ) -> RunSnapshot:
        resume_key = self._resume_key(discovery, objective, outcome_id)
        with Session(self._engine) as session, session.begin():
            run = session.scalar(select(RunRow).where(RunRow.resume_key == resume_key))
            resumed = run is not None
            if run is None:
                now = utc_now().isoformat()
                run = RunRow(
                    id=str(new_id()),
                    resume_key=resume_key,
                    repository_id=discovery.binding.repository_id,
                    repository_revision=discovery.binding.base_revision,
                    discovery_fingerprint=discovery.fingerprint,
                    objective=objective,
                    outcome_id=outcome_id,
                    status=RunState.PLANNING.value,
                    cancellation_requested=False,
                    created_at=now,
                    updated_at=now,
                )
                session.add(run)
                self._add_event(
                    session,
                    run.id,
                    "run.started",
                    {"repository_revision": discovery.binding.base_revision},
                )
            session.flush()
            return self._snapshot(session, run, resumed=resumed)

    def accept_plan(self, run_id: str, plan: AcceptedPlan) -> RunSnapshot:
        with Session(self._engine) as session, session.begin():
            run = self._require_run(session, run_id)
            existing = session.scalar(select(PlanRow).where(PlanRow.run_id == run_id))
            payload = plan.model_dump_json()
            if existing is not None:
                if existing.payload != payload:
                    raise MishkanError(
                        ErrorCode.DUPLICATE_RESULT,
                        "run already has a different accepted plan",
                        details={"run_id": run_id},
                    )
                return self._snapshot(session, run, resumed=True)

            session.add(
                PlanRow(
                    id=str(new_id()),
                    run_id=run_id,
                    fingerprint=plan.fingerprint,
                    payload=payload,
                    accepted_at=utc_now().isoformat(),
                )
            )
            for position, task in enumerate(plan.tasks):
                task_state = TaskState.ELIGIBLE if not task.depends_on else TaskState.PENDING
                session.add(
                    TaskRow(
                        id=str(new_id()),
                        run_id=run_id,
                        task_key=task.task_id,
                        position=position,
                        status=task_state.value,
                        revision=0,
                        attempt_count=0,
                        contract=task.model_dump_json(),
                        updated_at=utc_now().isoformat(),
                    )
                )
            run.status = RunState.RUNNING.value
            run.updated_at = utc_now().isoformat()
            self._add_event(
                session,
                run_id,
                "plan.accepted",
                {"plan_fingerprint": plan.fingerprint},
            )
            session.flush()
            return self._snapshot(session, run, resumed=False)

    def claim_task(self, run_id: str, task_id: str) -> int:
        """Move one eligible task to executing and return its monotone attempt number."""
        with Session(self._engine) as session, session.begin():
            run = self._require_run(session, run_id)
            task = self._require_task(session, run_id, task_id)
            if run.cancellation_requested or run.status != RunState.RUNNING.value:
                raise MishkanError(ErrorCode.RUN_INTERRUPTED, "run is not accepting task claims")
            if task.status != TaskState.ELIGIBLE.value:
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH,
                    "task is not eligible for execution",
                    details={"task_id": task_id, "state": task.status},
                )
            task.status = TaskState.EXECUTING.value
            task.attempt_count += 1
            task.revision += 1
            task.updated_at = utc_now().isoformat()
            self._add_event(
                session,
                run_id,
                "task.claimed",
                {"task_id": task_id, "attempt": task.attempt_count},
            )
            return task.attempt_count

    def mark_validating(self, run_id: str, task_id: str) -> None:
        with Session(self._engine) as session, session.begin():
            task = self._require_task(session, run_id, task_id)
            if task.status != TaskState.EXECUTING.value:
                raise MishkanError(ErrorCode.REVISION_MISMATCH, "task is not executing")
            task.status = TaskState.VALIDATING.value
            task.revision += 1
            task.updated_at = utc_now().isoformat()
            self._add_event(session, run_id, "task.validating", {"task_id": task_id})

    def mark_task_failure(self, run_id: str, task_id: str, *, rejected: bool) -> None:
        with Session(self._engine) as session, session.begin():
            task = self._require_task(session, run_id, task_id)
            if task.status not in {TaskState.EXECUTING.value, TaskState.VALIDATING.value}:
                raise MishkanError(ErrorCode.REVISION_MISMATCH, "task is not active")
            state = TaskState.REJECTED if rejected else TaskState.FAILED
            task.status = state.value
            task.revision += 1
            task.updated_at = utc_now().isoformat()
            self._add_event(session, run_id, f"task.{state.value}", {"task_id": task_id})

    def cancel_run(self, run_id: str) -> RunSnapshot:
        """Persist cancellation before preventing all future eligibility."""
        with Session(self._engine) as session, session.begin():
            run = self._require_run(session, run_id)
            if run.status in {RunState.COMPLETED.value, RunState.CANCELLED.value}:
                return self._snapshot(session, run, resumed=True)
            run.cancellation_requested = True
            run.status = RunState.CANCELLING.value
            now = utc_now().isoformat()
            run.updated_at = now
            tasks = session.scalars(select(TaskRow).where(TaskRow.run_id == run_id)).all()
            for task in tasks:
                if task.status in {TaskState.PENDING.value, TaskState.ELIGIBLE.value}:
                    task.status = TaskState.CANCELLED.value
                    task.revision += 1
                    task.updated_at = now
            if not any(
                task.status in {TaskState.EXECUTING.value, TaskState.VALIDATING.value}
                for task in tasks
            ):
                run.status = RunState.CANCELLED.value
            self._add_event(session, run_id, "run.cancellation_requested", {})
            return self._snapshot(session, run, resumed=False)

    def recover_interrupted(
        self,
        run_id: str,
        *,
        uncertain_effects: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        """Release interrupted work only after callers reconcile every stateful effect."""
        if uncertain_effects:
            raise MishkanError(
                ErrorCode.RUN_INTERRUPTED,
                "run contains unreconciled stateful effects",
                details={"effects": uncertain_effects, "automatic_retry": False},
            )
        with Session(self._engine) as session, session.begin():
            run = self._require_run(session, run_id)
            if run.cancellation_requested:
                return ()
            tasks = session.scalars(
                select(TaskRow).where(
                    TaskRow.run_id == run_id,
                    TaskRow.status.in_(
                        (
                            TaskState.EXECUTING.value,
                            TaskState.VALIDATING.value,
                            TaskState.REJECTED.value,
                            TaskState.FAILED.value,
                        )
                    ),
                )
            ).all()
            released: list[str] = []
            for task in tasks:
                if self._dependencies_accepted(session, run_id, task):
                    task.status = TaskState.ELIGIBLE.value
                    task.revision += 1
                    task.updated_at = utc_now().isoformat()
                    released.append(task.task_key)
            if released:
                self._add_event(
                    session, run_id, "run.interrupted_tasks_released", {"tasks": released}
                )
            return tuple(released)

    def task_states(self, run_id: str) -> dict[str, str]:
        with Session(self._engine) as session:
            rows = session.execute(
                select(TaskRow.task_key, TaskRow.status)
                .where(TaskRow.run_id == run_id)
                .order_by(TaskRow.position)
            ).all()
            return {row.task_key: row.status for row in rows}

    def accept_result(
        self,
        run_id: str,
        result: InitializationResult,
        review: ReviewDecision,
    ) -> RunSnapshot:
        with Session(self._engine) as session, session.begin():
            run = self._require_run(session, run_id)
            task = session.scalar(
                select(TaskRow).where(
                    TaskRow.run_id == run_id,
                    TaskRow.task_key == result.task_id,
                )
            )
            if task is None:
                raise MishkanError(
                    ErrorCode.OUTPUT_CONTRACT,
                    "result does not identify an accepted task",
                    details={"run_id": run_id, "task_id": result.task_id},
                )
            if result.repository_revision != run.repository_revision:
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH,
                    "result revision differs from the run base revision",
                    details={"run_id": run_id, "task_id": result.task_id},
                )

            payload = result.model_dump_json()
            existing = session.scalar(
                select(ResultRow).where(
                    ResultRow.run_id == run_id,
                    ResultRow.task_key == result.task_id,
                )
            )
            if existing is not None:
                if existing.payload != payload:
                    raise MishkanError(
                        ErrorCode.DUPLICATE_RESULT,
                        "task already has a different accepted result",
                        details={"run_id": run_id, "task_id": result.task_id},
                    )
                acceptance = session.scalar(
                    select(AcceptanceRow).where(
                        AcceptanceRow.run_id == run_id,
                        AcceptanceRow.task_key == result.task_id,
                    )
                )
                if acceptance is None or acceptance.review_payload != review.model_dump_json():
                    raise MishkanError(
                        ErrorCode.DUPLICATE_RESULT,
                        "task already has different review evidence",
                        details={"run_id": run_id, "task_id": result.task_id},
                    )
                return self._snapshot(session, run, resumed=True)

            if run.cancellation_requested:
                raise MishkanError(
                    ErrorCode.RUN_INTERRUPTED,
                    "run cancellation prevents new result acceptance",
                    details={"run_id": run_id},
                )
            if not self._dependencies_accepted(session, run_id, task):
                raise MishkanError(
                    ErrorCode.RUN_INTERRUPTED,
                    "task dependencies are not durably accepted",
                    details={"run_id": run_id, "task_id": result.task_id},
                )
            if review.verdict != "accepted":
                raise MishkanError(
                    ErrorCode.OUTPUT_CONTRACT,
                    "rejected review cannot become a durable task acceptance",
                )

            result_id = str(new_id())
            accepted_at = utc_now().isoformat()
            session.add(
                ResultRow(
                    id=result_id,
                    run_id=run_id,
                    task_key=result.task_id,
                    payload=payload,
                    accepted_at=accepted_at,
                )
            )
            session.flush()
            session.add(
                AcceptanceRow(
                    id=str(new_id()),
                    run_id=run_id,
                    task_key=result.task_id,
                    result_id=result_id,
                    review_payload=review.model_dump_json(),
                    accepted_at=accepted_at,
                )
            )
            task.status = TaskState.ACCEPTED.value
            task.revision += 1
            task.updated_at = accepted_at
            self._add_event(
                session,
                run_id,
                "task.result_accepted",
                {"task_id": result.task_id},
            )
            self._release_dependents(session, run_id)
            pending = session.scalar(
                select(TaskRow.id).where(
                    TaskRow.run_id == run_id,
                    TaskRow.status != TaskState.ACCEPTED.value,
                )
            )
            if pending is None:
                run.status = RunState.COMPLETED.value
                run.updated_at = accepted_at
                self._add_event(session, run_id, "run.completed", {})
            session.flush()
            return self._snapshot(session, run, resumed=False)

    def outbox_events(self) -> tuple[dict[str, Any], ...]:
        with Session(self._engine) as session:
            rows = session.scalars(select(OutboxRow).order_by(OutboxRow.cursor)).all()
            return tuple(
                {
                    "cursor": row.cursor,
                    "id": row.id,
                    "aggregate_id": row.aggregate_id,
                    "entity_type": row.entity_type,
                    "event_type": row.event_type,
                    "source": row.source,
                    "payload": json.loads(row.payload),
                    "occurred_at": row.occurred_at,
                }
                for row in rows
            )

    def record(self, audit: AuditEvent) -> None:
        """Persist already-inspected capability evidence in the authoritative outbox."""
        with Session(self._engine) as session, session.begin():
            self._require_run(session, audit.run_id)
            session.add(
                OutboxRow(
                    id=str(audit.id),
                    schema_version=audit.schema_version,
                    aggregate_id=audit.run_id,
                    entity_type="run",
                    event_type=audit.event_type,
                    source="mishkan.gateway",
                    payload=audit.model_dump_json(),
                    occurred_at=audit.created_at.isoformat(),
                    command_id=None,
                    correlation_id=None,
                    causation_id=None,
                    sensitivity="internal",
                    published_at=None,
                )
            )

    @staticmethod
    def _resume_key(
        discovery: DiscoverySnapshot,
        objective: str,
        outcome_id: str,
    ) -> str:
        source = "\0".join(
            (
                discovery.binding.repository_id,
                discovery.binding.base_revision,
                discovery.fingerprint,
                objective,
                outcome_id,
            )
        )
        return hashlib.sha256(source.encode()).hexdigest()

    @staticmethod
    def _require_run(session: Session, run_id: str) -> RunRow:
        run = session.get(RunRow, run_id)
        if run is None:
            raise MishkanError(
                ErrorCode.RUN_INTERRUPTED,
                "run state does not exist",
                details={"run_id": run_id},
            )
        return run

    @staticmethod
    def _require_task(session: Session, run_id: str, task_id: str) -> TaskRow:
        task = session.scalar(
            select(TaskRow).where(TaskRow.run_id == run_id, TaskRow.task_key == task_id)
        )
        if task is None:
            raise MishkanError(
                ErrorCode.RUN_INTERRUPTED,
                "run task does not exist",
                details={"run_id": run_id, "task_id": task_id},
            )
        return task

    @staticmethod
    def _dependencies_accepted(session: Session, run_id: str, task: TaskRow) -> bool:
        contract = PlanTask.model_validate_json(task.contract)
        if not contract.depends_on:
            return True
        accepted = set(
            session.scalars(
                select(TaskRow.task_key).where(
                    TaskRow.run_id == run_id,
                    TaskRow.task_key.in_(contract.depends_on),
                    TaskRow.status == TaskState.ACCEPTED.value,
                )
            ).all()
        )
        return accepted == set(contract.depends_on)

    @classmethod
    def _release_dependents(cls, session: Session, run_id: str) -> None:
        pending = session.scalars(
            select(TaskRow).where(
                TaskRow.run_id == run_id,
                TaskRow.status == TaskState.PENDING.value,
            )
        ).all()
        for task in pending:
            if cls._dependencies_accepted(session, run_id, task):
                task.status = TaskState.ELIGIBLE.value
                task.revision += 1
                task.updated_at = utc_now().isoformat()

    @staticmethod
    def _snapshot(session: Session, run: RunRow, *, resumed: bool) -> RunSnapshot:
        plan_row = session.scalar(select(PlanRow).where(PlanRow.run_id == run.id))
        result_rows = session.scalars(
            select(ResultRow).where(ResultRow.run_id == run.id).order_by(ResultRow.accepted_at)
        ).all()
        acceptance_rows = session.scalars(
            select(AcceptanceRow)
            .where(AcceptanceRow.run_id == run.id)
            .order_by(AcceptanceRow.accepted_at)
        ).all()
        plan = AcceptedPlan.model_validate_json(plan_row.payload) if plan_row is not None else None
        results = tuple(
            InitializationResult.model_validate_json(row.payload) for row in result_rows
        )
        reviews = tuple(
            ReviewDecision.model_validate_json(row.review_payload) for row in acceptance_rows
        )
        return RunSnapshot(
            run_id=run.id,
            resumed=resumed,
            plan=plan,
            results=results,
            reviews=reviews,
        )

    @staticmethod
    def _add_event(
        session: Session,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        session.add(
            OutboxRow(
                id=str(new_id()),
                schema_version="1.0",
                aggregate_id=aggregate_id,
                entity_type="run",
                event_type=event_type,
                source="mishkan.runtime",
                payload=json.dumps(payload, sort_keys=True, separators=(",", ":")),
                occurred_at=datetime.isoformat(utc_now()),
                command_id=None,
                correlation_id=None,
                causation_id=None,
                sensitivity="internal",
                published_at=None,
            )
        )
