"""SQLAlchemy repository with SQLite/WAL and transactional outbox semantics."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.identity import new_id
from mishkan.domain.time import utc_now
from mishkan.planning.models import AcceptedPlan, InitializationResult, ReviewDecision
from mishkan.repository.models import DiscoverySnapshot
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
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
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
    contract: Mapped[str] = mapped_column(Text, nullable=False)


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

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    aggregate_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[str] = mapped_column(String(40), nullable=False)
    published_at: Mapped[str | None] = mapped_column(String(40))


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
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{database_path}")
        event.listen(self._engine, "connect", self._configure_connection)
        Base.metadata.create_all(self._engine)

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
                run = RunRow(
                    id=str(new_id()),
                    resume_key=resume_key,
                    repository_id=discovery.binding.repository_id,
                    repository_revision=discovery.binding.base_revision,
                    discovery_fingerprint=discovery.fingerprint,
                    objective=objective,
                    outcome_id=outcome_id,
                    status="planning",
                    created_at=utc_now().isoformat(),
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
                session.add(
                    TaskRow(
                        id=str(new_id()),
                        run_id=run_id,
                        task_key=task.task_id,
                        position=position,
                        status="pending",
                        contract=task.model_dump_json(),
                    )
                )
            run.status = "running"
            self._add_event(
                session,
                run_id,
                "plan.accepted",
                {"plan_fingerprint": plan.fingerprint},
            )
            session.flush()
            return self._snapshot(session, run, resumed=False)

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
            task.status = "completed"
            self._add_event(
                session,
                run_id,
                "task.result_accepted",
                {"task_id": result.task_id},
            )
            pending = session.scalar(
                select(TaskRow.id).where(TaskRow.run_id == run_id, TaskRow.status != "completed")
            )
            if pending is None:
                run.status = "completed"
                self._add_event(session, run_id, "run.completed", {})
            session.flush()
            return self._snapshot(session, run, resumed=False)

    def outbox_events(self) -> tuple[dict[str, Any], ...]:
        with Session(self._engine) as session:
            rows = session.scalars(select(OutboxRow).order_by(OutboxRow.occurred_at)).all()
            return tuple(
                {
                    "id": row.id,
                    "aggregate_id": row.aggregate_id,
                    "event_type": row.event_type,
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
                    aggregate_id=audit.run_id,
                    event_type=audit.event_type,
                    payload=audit.model_dump_json(),
                    occurred_at=audit.created_at.isoformat(),
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
                aggregate_id=aggregate_id,
                event_type=event_type,
                payload=json.dumps(payload, sort_keys=True, separators=(",", ":")),
                occurred_at=datetime.isoformat(utc_now()),
                published_at=None,
            )
        )
