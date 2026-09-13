from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.persistence import SchemaManager
from mishkan.skills import (
    SkillSelection,
    SkillSelectionContext,
    SkillUsageRecord,
    SkillUseOutcome,
)
from mishkan.skills.repository import SQLiteSkillUsageRepository


def _record(*, outcome: SkillUseOutcome = SkillUseOutcome.MISS) -> SkillUsageRecord:
    selection = SkillSelection(
        requested_name="missing-review",
        context=SkillSelectionContext(
            task_id="task-1",
            task_class="software.review",
            consuming_identity="quality-engineer",
            platform="linux",
            organization_version="organization:59@1",
        ),
        outcome=outcome,
        reason="no compatible active package",
    )
    return SkillUsageRecord(
        task_id="task-1",
        task_class="software.review",
        consuming_identity="quality-engineer",
        requested_skill="missing-review",
        outcome=outcome,
        reason=selection.reason,
        policy_fingerprint="a" * 64,
        evidence=selection,
    )


def test_skill_usage_is_durable_idempotent_and_emits_one_fact(tmp_path: Path) -> None:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    repository = SQLiteSkillUsageRepository(database)
    record = _record()

    assert repository.record(record) == record
    assert repository.record(record) == record
    summary = SQLiteSkillUsageRepository(database).summary(
        "software.review", requested_skill="missing-review"
    )

    assert summary.hits == 0
    assert summary.partials == 0
    assert summary.misses == 1
    assert summary.last_recorded_at == record.recorded_at
    with create_engine(f"sqlite:///{database}").connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM event_outbox")).scalar_one() == 1


def test_conflicting_skill_usage_identity_is_refused(tmp_path: Path) -> None:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    repository = SQLiteSkillUsageRepository(database)
    record = _record()
    repository.record(record)

    with pytest.raises(MishkanError) as caught:
        repository.record(record.model_copy(update={"reason": "different observation"}))

    assert caught.value.envelope.code is ErrorCode.DUPLICATE_RESULT


def test_usage_evidence_must_match_the_record_context() -> None:
    record = _record()

    with pytest.raises(ValueError, match="another task context"):
        SkillUsageRecord(
            task_id="another-task",
            task_class=record.task_class,
            consuming_identity=record.consuming_identity,
            requested_skill=record.requested_skill,
            outcome=record.outcome,
            reason=record.reason,
            policy_fingerprint=record.policy_fingerprint,
            evidence=record.evidence,
        )
