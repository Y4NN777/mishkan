from __future__ import annotations

from typing import Any

import pytest

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.skills import (
    SkillInvocationRequest,
    SkillSelection,
    SkillSelectionContext,
    SkillUseOutcome,
)
from mishkan.skills.service import SkillInvocationService


class _Lifecycle:
    def active(self, _name: str) -> None:
        return None

    def list_versions(self, *, offset: int, limit: int) -> tuple[object, ...]:
        assert (offset, limit) == (0, 1_000)
        return ()


class _Usage:
    def __init__(self) -> None:
        self.records: list[Any] = []

    def record(self, value: object) -> None:
        self.records.append(value)


def _request(**updates: Any) -> SkillInvocationRequest:
    request = SkillInvocationRequest(
        context=SkillSelectionContext(
            task_id="task-skill-miss",
            task_class="software.review.python",
            consuming_identity="engineer:test",
            platform="linux",
            organization_version="org:test",
        )
    )
    return request.model_copy(update=updates)


@pytest.mark.parametrize(
    ("invocation", "automatic", "requested_name"),
    [
        (_request(requested_name="missing-skill"), True, "missing-skill"),
        (_request(bundle_id="missing.bundle"), True, "missing.bundle"),
        (_request(), False, "automatic"),
        (_request(), True, "automatic"),
    ],
)
def test_invocation_records_each_proven_resolution_miss(
    invocation: SkillInvocationRequest,
    automatic: bool,
    requested_name: str,
) -> None:
    usage = _Usage()
    service = SkillInvocationService(
        _Lifecycle(),  # type: ignore[arg-type]
        usage,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        bundles=(),
        automatic_selection=automatic,
        max_automatic_skills=2,
    )

    evidence = service.invoke(invocation, policy_fingerprint="a" * 64)

    assert evidence.outcome is SkillUseOutcome.MISS
    assert evidence.load_evidence == ()
    assert evidence.selections[0].requested_name == requested_name
    assert len(usage.records) == 1
    assert usage.records[0].requested_skill == requested_name


def test_selection_recording_rejects_non_miss_evidence() -> None:
    service = SkillInvocationService(
        _Lifecycle(),  # type: ignore[arg-type]
        _Usage(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        bundles=(),
        automatic_selection=False,
        max_automatic_skills=1,
    )
    invalid = SkillSelection.model_construct(
        requested_name="invalid",
        context=_request().context,
        outcome=SkillUseOutcome.HIT,
        selected=None,
        reason="invalid evidence",
        missing_conditions=(),
        fallback_bindings={},
    )

    with pytest.raises(MishkanError) as caught:
        service._record_selection(invalid, "b" * 64)
    assert caught.value.envelope.code is ErrorCode.SKILL_SELECTION
