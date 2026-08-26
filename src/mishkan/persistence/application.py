"""Transactional command ledger, typed event queries, and bounded snapshots."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mishkan.application import (
    ApplicationCommand,
    CommandResult,
    CommandStatus,
    SnapshotEnvelope,
)
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.identity import new_id
from mishkan.domain.time import utc_now
from mishkan.events import EventEnvelope, EventPage
from mishkan.persistence.migration import SchemaManager
from mishkan.persistence.sqlite import (
    AggregateRevisionRow,
    CommandRow,
    LocalRunRepository,
    OutboxRow,
    RunRow,
    TaskRow,
)


class SQLiteApplicationRepository:
    """Own command idempotence, optimistic revisions, and the durable event cursor."""

    def __init__(self, database_path: Path) -> None:
        SchemaManager(database_path).require_current()
        self._engine = create_engine(f"sqlite:///{database_path}")
        event.listen(self._engine, "connect", LocalRunRepository._configure_connection)

    def accept(
        self,
        command: ApplicationCommand,
        *,
        target_id: str,
        event_type: str,
        result_payload: Mapping[str, Any] | None = None,
        event_payload: Mapping[str, Any] | None = None,
        source: str = "mishkan.application",
        sensitivity: str = "internal",
    ) -> CommandResult:
        try:
            with Session(self._engine) as session, session.begin():
                existing = session.get(CommandRow, str(command.command_id))
                if existing is not None:
                    return self._existing(existing, command)
                revision_row = session.get(AggregateRevisionRow, (command.target_type, target_id))
                current_revision = revision_row.revision if revision_row is not None else 0
                if (
                    command.expected_revision is not None
                    and command.expected_revision != current_revision
                ):
                    raise MishkanError(
                        ErrorCode.REVISION_MISMATCH,
                        "application command expected a stale aggregate revision",
                        details={
                            "target_type": command.target_type,
                            "target_id": target_id,
                            "expected": command.expected_revision,
                            "current": current_revision,
                        },
                    )
                next_revision = current_revision + 1
                now = utc_now()
                if revision_row is None:
                    session.add(
                        AggregateRevisionRow(
                            entity_type=command.target_type,
                            entity_id=target_id,
                            revision=next_revision,
                            updated_at=now.isoformat(),
                        )
                    )
                else:
                    revision_row.revision = next_revision
                    revision_row.updated_at = now.isoformat()
                event_row = OutboxRow(
                    id=str(new_id()),
                    schema_version="1.0",
                    aggregate_id=target_id,
                    entity_type=command.target_type,
                    event_type=event_type,
                    source=source,
                    payload=json.dumps(
                        dict(event_payload or {}), sort_keys=True, separators=(",", ":")
                    ),
                    occurred_at=now.isoformat(),
                    command_id=str(command.command_id),
                    correlation_id=str(command.command_id),
                    causation_id=None,
                    sensitivity=sensitivity,
                    published_at=None,
                )
                session.add(event_row)
                session.flush()
                result = CommandResult(
                    command_id=command.command_id,
                    status=CommandStatus.ACCEPTED,
                    target_type=command.target_type,
                    target_id=target_id,
                    revision=next_revision,
                    event_cursor=event_row.cursor,
                    payload=dict(result_payload or {}),
                    completed_at=now,
                )
                session.add(self._command_row(command, result))
                session.flush()
                return result
        except IntegrityError as exc:
            with Session(self._engine) as session:
                existing = session.get(CommandRow, str(command.command_id))
                if existing is not None:
                    return self._existing(existing, command)
            raise MishkanError(
                ErrorCode.DUPLICATE_RESULT,
                "application command could not be committed uniquely",
            ) from exc

    def reserve(self, command: ApplicationCommand, *, target_id: str) -> CommandResult | None:
        """Durably reserve an effectful command before executing it.

        A daemon crash leaves a stable refused receipt. Replaying that UUID therefore
        fails closed instead of repeating an effect whose outcome has not been
        reconciled.
        """
        with Session(self._engine) as session, session.begin():
            existing = session.get(CommandRow, str(command.command_id))
            if existing is not None:
                return self._existing(existing, command)
            revision_row = session.get(AggregateRevisionRow, (command.target_type, target_id))
            current_revision = revision_row.revision if revision_row is not None else 0
            if (
                command.expected_revision is not None
                and command.expected_revision != current_revision
            ):
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH,
                    "application command expected a stale aggregate revision",
                    details={
                        "target_type": command.target_type,
                        "target_id": target_id,
                        "expected": command.expected_revision,
                        "current": current_revision,
                    },
                )
            now = utc_now()
            interrupted = MishkanError(
                ErrorCode.RUN_INTERRUPTED,
                "command was interrupted before its effect could be accepted",
                details={
                    "command_id": str(command.command_id),
                    "reconciliation_required": True,
                    "automatic_retry": False,
                },
            )
            result = CommandResult(
                command_id=command.command_id,
                status=CommandStatus.REFUSED,
                target_type=command.target_type,
                target_id=target_id,
                error=interrupted.envelope,
                completed_at=now,
            )
            session.add(self._command_row(command, result))
            return None

    def complete_reserved(
        self,
        command: ApplicationCommand,
        *,
        target_id: str,
        event_type: str,
        result_payload: Mapping[str, Any] | None = None,
        event_payload: Mapping[str, Any] | None = None,
        source: str = "mishkan.application",
        sensitivity: str = "internal",
    ) -> CommandResult:
        """Atomically accept one reserved command, its revision, and its event."""
        with Session(self._engine) as session, session.begin():
            row = session.get(CommandRow, str(command.command_id))
            if row is None:
                raise MishkanError(ErrorCode.RUN_INTERRUPTED, "command was not reserved")
            if row.fingerprint != command.fingerprint:
                raise MishkanError(
                    ErrorCode.DUPLICATE_RESULT,
                    "command identity was already used for different content",
                    details={"command_id": str(command.command_id)},
                )
            current_result = CommandResult.model_validate_json(row.result_payload)
            if current_result.status is CommandStatus.ACCEPTED:
                return current_result
            if (
                current_result.status is not CommandStatus.REFUSED
                or current_result.error is None
                or not current_result.error.details.get("reconciliation_required")
            ):
                return current_result
            revision_row = session.get(AggregateRevisionRow, (command.target_type, target_id))
            current_revision = revision_row.revision if revision_row is not None else 0
            if (
                command.expected_revision is not None
                and command.expected_revision != current_revision
            ):
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH,
                    "aggregate revision changed while command effect was executing",
                    details={
                        "target_type": command.target_type,
                        "target_id": target_id,
                        "expected": command.expected_revision,
                        "current": current_revision,
                        "effect_reconciliation_required": True,
                    },
                )
            next_revision = current_revision + 1
            now = utc_now()
            if revision_row is None:
                session.add(
                    AggregateRevisionRow(
                        entity_type=command.target_type,
                        entity_id=target_id,
                        revision=next_revision,
                        updated_at=now.isoformat(),
                    )
                )
            else:
                revision_row.revision = next_revision
                revision_row.updated_at = now.isoformat()
            event_row = OutboxRow(
                id=str(new_id()),
                schema_version="1.0",
                aggregate_id=target_id,
                entity_type=command.target_type,
                event_type=event_type,
                source=source,
                payload=json.dumps(
                    dict(event_payload or {}), sort_keys=True, separators=(",", ":")
                ),
                occurred_at=now.isoformat(),
                command_id=str(command.command_id),
                correlation_id=str(command.command_id),
                causation_id=None,
                sensitivity=sensitivity,
                published_at=None,
            )
            session.add(event_row)
            session.flush()
            result = CommandResult(
                command_id=command.command_id,
                status=CommandStatus.ACCEPTED,
                target_type=command.target_type,
                target_id=target_id,
                revision=next_revision,
                event_cursor=event_row.cursor,
                payload=dict(result_payload or {}),
                completed_at=now,
            )
            row.status = result.status.value
            row.result_payload = result.model_dump_json()
            row.event_cursor = result.event_cursor
            row.completed_at = result.completed_at.isoformat()
            return result

    def command_result(self, command_id: str) -> CommandResult | None:
        with Session(self._engine) as session:
            row = session.get(CommandRow, command_id)
            return (
                CommandResult.model_validate_json(row.result_payload) if row is not None else None
            )

    def replay(self, command: ApplicationCommand) -> CommandResult | None:
        """Return an identical command result or reject reuse of its UUID."""
        with Session(self._engine) as session:
            row = session.get(CommandRow, str(command.command_id))
            return self._existing(row, command) if row is not None else None

    def require_expected_revision(self, command: ApplicationCommand, target_id: str) -> None:
        if command.expected_revision is None:
            return
        with Session(self._engine) as session:
            row = session.get(AggregateRevisionRow, (command.target_type, target_id))
            current = row.revision if row is not None else 0
        if current != command.expected_revision:
            raise MishkanError(
                ErrorCode.REVISION_MISMATCH,
                "application command expected a stale aggregate revision",
                details={"expected": command.expected_revision, "current": current},
            )

    def events(
        self,
        *,
        after_cursor: int = 0,
        limit: int = 100,
        event_types: tuple[str, ...] = (),
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> EventPage:
        if limit < 1 or limit > 1_000:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "event query limit is outside the supported bound",
                details={"limit": limit, "minimum": 1, "maximum": 1000},
            )
        with Session(self._engine) as session:
            retained = session.scalar(select(func.min(OutboxRow.cursor))) or 0
            if retained and after_cursor and after_cursor < retained - 1:
                raise MishkanError(
                    ErrorCode.RUN_INTERRUPTED,
                    "event cursor is older than the retained stream",
                    details={
                        "category": "cursor_gap",
                        "after_cursor": after_cursor,
                        "retained_from_cursor": retained,
                        "snapshot_required": True,
                    },
                )
            statement = select(OutboxRow).where(OutboxRow.cursor > after_cursor)
            if event_types:
                statement = statement.where(OutboxRow.event_type.in_(event_types))
            if entity_type is not None:
                statement = statement.where(OutboxRow.entity_type == entity_type)
            if entity_id is not None:
                statement = statement.where(OutboxRow.aggregate_id == entity_id)
            rows = session.scalars(statement.order_by(OutboxRow.cursor).limit(limit)).all()
            events = tuple(self._event(row) for row in rows)
            return EventPage(
                after_cursor=after_cursor,
                next_cursor=events[-1].cursor if events else after_cursor,
                retained_from_cursor=retained,
                events=events,
            )

    def snapshot(self, *, limit: int = 1_000) -> SnapshotEnvelope:
        self._query_bound(0, limit)
        with Session(self._engine) as session, session.begin():
            cursor = session.scalar(select(func.max(OutboxRow.cursor))) or 0
            runs = session.execute(
                select(RunRow.id, RunRow.status, RunRow.revision)
                .order_by(RunRow.created_at)
                .limit(limit)
            ).all()
            tasks = session.execute(
                select(TaskRow.id, TaskRow.run_id, TaskRow.status, TaskRow.revision)
                .order_by(TaskRow.run_id, TaskRow.position)
                .limit(limit)
            ).all()
            return SnapshotEnvelope(
                cursor=cursor,
                projections={
                    "runs": [
                        {"id": row.id, "status": row.status, "revision": row.revision}
                        for row in runs
                    ],
                    "tasks": [
                        {
                            "id": row.id,
                            "run_id": row.run_id,
                            "status": row.status,
                            "revision": row.revision,
                        }
                        for row in tasks
                    ],
                },
            )

    def runs(self, *, offset: int = 0, limit: int = 100) -> tuple[dict[str, Any], ...]:
        self._query_bound(offset, limit)
        with Session(self._engine) as session:
            rows = session.scalars(
                select(RunRow).order_by(RunRow.created_at, RunRow.id).offset(offset).limit(limit)
            ).all()
            return tuple(
                {
                    "id": row.id,
                    "status": row.status,
                    "revision": row.revision,
                    "cancellation_requested": row.cancellation_requested,
                    "repository_revision": row.repository_revision,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                }
                for row in rows
            )

    def tasks(
        self,
        run_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        self._query_bound(offset, limit)
        with Session(self._engine) as session:
            rows = session.scalars(
                select(TaskRow)
                .where(TaskRow.run_id == run_id)
                .order_by(TaskRow.position)
                .offset(offset)
                .limit(limit)
            ).all()
            return tuple(
                {
                    "id": row.id,
                    "task_id": row.task_key,
                    "status": row.status,
                    "revision": row.revision,
                    "attempt_count": row.attempt_count,
                    "updated_at": row.updated_at,
                }
                for row in rows
            )

    @staticmethod
    def _query_bound(offset: int, limit: int) -> None:
        if offset < 0 or limit < 1 or limit > 1_000:
            raise MishkanError(ErrorCode.OUTPUT_CONTRACT, "query bound is invalid")

    @staticmethod
    def _command_row(command: ApplicationCommand, result: CommandResult) -> CommandRow:
        return CommandRow(
            command_id=str(command.command_id),
            schema_version=command.schema_version,
            fingerprint=command.fingerprint,
            command_type=command.command_type,
            actor_id=command.actor_id,
            target_type=command.target_type,
            target_id=result.target_id,
            expected_revision=command.expected_revision,
            issued_at=command.issued_at.isoformat(),
            status=result.status.value,
            result_payload=result.model_dump_json(),
            event_cursor=result.event_cursor,
            completed_at=result.completed_at.isoformat(),
        )

    @staticmethod
    def _existing(row: CommandRow, command: ApplicationCommand) -> CommandResult:
        if row.fingerprint != command.fingerprint:
            raise MishkanError(
                ErrorCode.DUPLICATE_RESULT,
                "command identity was already used for different content",
                details={"command_id": str(command.command_id)},
            )
        return CommandResult.model_validate_json(row.result_payload)

    @staticmethod
    def _event(row: OutboxRow) -> EventEnvelope:
        return EventEnvelope(
            event_id=UUID(row.id),
            cursor=row.cursor,
            schema_version=row.schema_version,
            event_type=row.event_type,
            source=row.source,
            entity_type=row.entity_type,
            entity_id=row.aggregate_id,
            occurred_at=datetime.fromisoformat(row.occurred_at),
            command_id=UUID(row.command_id) if row.command_id is not None else None,
            correlation_id=(UUID(row.correlation_id) if row.correlation_id is not None else None),
            causation_id=UUID(row.causation_id) if row.causation_id is not None else None,
            sensitivity=row.sensitivity,
            payload=json.loads(row.payload),
        )
