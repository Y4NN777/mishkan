"""Durable skill-use evidence and bounded miss aggregation."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.identity import new_id
from mishkan.persistence.migration import SchemaManager
from mishkan.persistence.sqlite import OutboxRow, SkillUsageRow, create_local_engine
from mishkan.skills.models import SkillUsageRecord, SkillUsageSummary, SkillUseOutcome


class SQLiteSkillUsageRepository:
    """Persist usage facts without turning aggregate thresholds into activation."""

    def __init__(self, database_path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        SchemaManager(database_path).require_current()
        self._engine = create_local_engine(database_path, busy_timeout_ms=busy_timeout_ms)

    def record(self, record: SkillUsageRecord) -> SkillUsageRecord:
        payload = self._payload(record)
        with Session(self._engine) as session, session.begin():
            existing = session.get(SkillUsageRow, str(record.id))
            if existing is not None:
                if existing.evidence_payload == payload:
                    return record
                raise MishkanError(
                    ErrorCode.DUPLICATE_RESULT,
                    "skill usage identity already contains different evidence",
                    details={"record_id": str(record.id)},
                )
            session.add(
                SkillUsageRow(
                    id=str(record.id),
                    task_id=record.task_id,
                    task_class=record.task_class,
                    consuming_identity=record.consuming_identity,
                    requested_skill=record.requested_skill,
                    skill_version=record.skill_version,
                    package_fingerprint=record.package_fingerprint,
                    outcome=record.outcome.value,
                    reason=record.reason,
                    policy_fingerprint=record.policy_fingerprint,
                    evidence_payload=payload,
                    recorded_at=record.recorded_at.isoformat(),
                )
            )
            session.add(
                OutboxRow(
                    id=str(new_id()),
                    schema_version="1.0",
                    aggregate_id=record.task_id,
                    entity_type="skill_usage",
                    run_id=None,
                    task_id=record.task_id,
                    identity_id=record.consuming_identity,
                    team_id=None,
                    security_relevant=False,
                    event_type=f"skill.usage_{record.outcome.value}",
                    source="mishkan.skills",
                    payload=json.dumps(
                        {
                            "record_id": str(record.id),
                            "requested_skill": record.requested_skill,
                            "task_class": record.task_class,
                            "outcome": record.outcome.value,
                            "policy_fingerprint": record.policy_fingerprint,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    occurred_at=record.recorded_at.isoformat(),
                    command_id=None,
                    correlation_id=None,
                    causation_id=None,
                    sensitivity="internal",
                    published_at=None,
                )
            )
        return record

    def summary(
        self,
        task_class: str,
        *,
        requested_skill: str | None = None,
    ) -> SkillUsageSummary:
        conditions = [SkillUsageRow.task_class == task_class]
        if requested_skill is not None:
            conditions.append(SkillUsageRow.requested_skill == requested_skill)
        with Session(self._engine) as session:
            rows = session.execute(
                select(
                    SkillUsageRow.outcome,
                    func.count(SkillUsageRow.id),
                    func.max(SkillUsageRow.recorded_at),
                )
                .where(*conditions)
                .group_by(SkillUsageRow.outcome)
            ).all()
        counts = {outcome: count for outcome, count, _ in rows}
        timestamps = [datetime.fromisoformat(timestamp) for _, _, timestamp in rows if timestamp]
        return SkillUsageSummary(
            task_class=task_class,
            requested_skill=requested_skill,
            hits=counts.get(SkillUseOutcome.HIT.value, 0),
            partials=counts.get(SkillUseOutcome.PARTIAL.value, 0),
            misses=counts.get(SkillUseOutcome.MISS.value, 0),
            last_recorded_at=max(timestamps) if timestamps else None,
        )

    @staticmethod
    def _payload(record: SkillUsageRecord) -> str:
        return json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
