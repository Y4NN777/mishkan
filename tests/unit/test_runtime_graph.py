from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.persistence import LocalRunRepository, SchemaManager
from mishkan.planning.models import (
    AcceptedPlan,
    InitializationResult,
    PlanTask,
    ReviewDecision,
)
from mishkan.repository.models import DiscoverySnapshot, RepositoryBinding
from mishkan.runtime import (
    BoundedPredicateLoop,
    PredicateEvaluator,
    PredicateLimits,
    RunState,
    TaskState,
)
from mishkan.tools.execution import EffectSettlement
from mishkan.tools.gateway_models import CallStatus, ToolResultEnvelope


def _discovery(tmp_path: Path) -> DiscoverySnapshot:
    return DiscoverySnapshot(
        binding=RepositoryBinding(
            repository_id="a" * 64,
            root=tmp_path,
            base_revision="b" * 40,
        ),
        facts=(),
        unknowns=(),
        fingerprint="c" * 64,
    )


def _plan() -> AcceptedPlan:
    return AcceptedPlan(
        objective="Execute a durable dependency graph",
        outcome_id="mission",
        repository_revision="b" * 40,
        tasks=(
            PlanTask(
                task_id="root-task",
                title="Produce root evidence",
                purpose="Produce accepted evidence for the dependent task.",
                assigned_role="Engineer",
                tools=("file.read",),
                evidence_paths=("README.md",),
            ),
            PlanTask(
                task_id="dependent-task",
                title="Consume accepted evidence",
                purpose="Run only after the root result is accepted.",
                assigned_role="Reviewer",
                tools=("file.read",),
                evidence_paths=("README.md",),
                depends_on=("root-task",),
            ),
        ),
        fingerprint="d" * 64,
        discovery_fingerprint="c" * 64,
    )


def _result(task_id: str) -> InitializationResult:
    return InitializationResult(
        repository_revision="b" * 40,
        task_id=task_id,
        summary=f"Accepted result for {task_id}.",
        cited_paths=("README.md",),
        findings=(f"Finding for {task_id}.",),
    )


def _review(task_id: str) -> ReviewDecision:
    return ReviewDecision(
        task_id=task_id,
        verdict="accepted",
        summary="Independent evidence accepted the result.",
        checked_citations=("README.md",),
    )


def _repository(tmp_path: Path) -> tuple[LocalRunRepository, str]:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    repository = LocalRunRepository(database)
    run = repository.start_or_resume(_discovery(tmp_path), "Execute graph", "mission")
    repository.mark_awaiting_approval(run.run_id)
    assert repository.run_state(run.run_id) == RunState.AWAITING_APPROVAL.value
    repository.accept_plan(run.run_id, _plan())
    assert repository.run_state(run.run_id) == RunState.QUEUED.value
    repository.start_run(run.run_id)
    return repository, run.run_id


def test_dependency_releases_only_in_same_transaction_as_acceptance(tmp_path: Path) -> None:
    repository, run_id = _repository(tmp_path)
    assert repository.task_states(run_id) == {
        "root-task": TaskState.ELIGIBLE.value,
        "dependent-task": TaskState.PENDING.value,
    }
    with pytest.raises(MishkanError) as blocked:
        repository.accept_result(run_id, _result("dependent-task"), _review("dependent-task"))
    assert blocked.value.envelope.code is ErrorCode.RUN_INTERRUPTED

    assert repository.claim_task(run_id, "root-task") == 1
    repository.mark_validating(run_id, "root-task")
    repository.accept_result(run_id, _result("root-task"), _review("root-task"))
    assert repository.task_states(run_id)["dependent-task"] == TaskState.ELIGIBLE.value


def test_duplicate_completion_has_no_duplicate_event_and_cancellation_is_monotone(
    tmp_path: Path,
) -> None:
    repository, run_id = _repository(tmp_path)
    repository.claim_task(run_id, "root-task")
    repository.mark_validating(run_id, "root-task")
    result = _result("root-task")
    review = _review("root-task")
    repository.accept_result(run_id, result, review)
    count = len(repository.outbox_events())
    repository.accept_result(run_id, result, review)
    duplicate = repository.outbox_events()[count]
    assert duplicate["event_type"] == "task.completion_duplicated"
    assert duplicate["payload"] == {"ignored": True, "task_id": "root-task"}

    repository.cancel_run(run_id)
    assert repository.task_states(run_id)["dependent-task"] == TaskState.CANCELLED.value
    with pytest.raises(MishkanError):
        repository.claim_task(run_id, "dependent-task")


def test_active_run_cancellation_is_signalled_then_durably_settled(tmp_path: Path) -> None:
    repository, run_id = _repository(tmp_path)
    repository.claim_task(run_id, "root-task")

    repository.cancel_run(run_id)

    assert repository.requested(run_id, "root-task:1") is True
    assert repository.run_state(run_id) == RunState.CANCELLING.value
    assert repository.task_states(run_id) == {
        "root-task": TaskState.EXECUTING.value,
        "dependent-task": TaskState.CANCELLED.value,
    }
    settled = repository.settle_cancellation(run_id)

    assert settled.run_id == run_id
    assert repository.run_state(run_id) == RunState.CANCELLED.value
    assert repository.task_states(run_id) == {
        "root-task": TaskState.CANCELLED.value,
        "dependent-task": TaskState.CANCELLED.value,
    }
    event_types = [event["event_type"] for event in repository.outbox_events()]
    assert "task.cancellation_requested" in event_types
    assert event_types[-2:] == ["task.cancelled", "run.cancelled"]
    assert repository.settle_cancellation(run_id).resumed is True


def test_interrupted_task_requires_effect_reconciliation_before_retry(tmp_path: Path) -> None:
    repository, run_id = _repository(tmp_path)
    repository.claim_task(run_id, "root-task")
    invocation_id = str(uuid4())
    request_fingerprint = "e" * 64
    assert (
        repository.reserve_planned_call(
            invocation_id=invocation_id,
            run_id=run_id,
            task_attempt_id="root-task:1",
            planned_call_id="mutate-repository",
            request_fingerprint=request_fingerprint,
            tool_id="git.commit",
            tool_version="1.0",
            effect_class="repository_write",
            declared_effects=("git.commit",),
        )
        is None
    )
    with pytest.raises(MishkanError) as reused:
        repository.reserve_planned_call(
            invocation_id=invocation_id,
            run_id=run_id,
            task_attempt_id="root-task:1",
            planned_call_id="mutate-repository",
            request_fingerprint="f" * 64,
            tool_id="git.commit",
            tool_version="1.0",
            effect_class="repository_write",
            declared_effects=("git.commit",),
        )
    assert reused.value.envelope.code is ErrorCode.DUPLICATE_RESULT
    repository.mark_planned_call_dispatching(invocation_id, request_fingerprint)
    with pytest.raises(MishkanError) as blocked:
        repository.recover_interrupted(run_id)
    assert blocked.value.envelope.details["automatic_retry"] is False
    assert blocked.value.envelope.details["effects"] == (
        "planned-call:root-task:1:mutate-repository:dispatching",
    )
    assert repository.run_state(run_id) == RunState.BLOCKED.value
    now = utc_now()
    repository.complete_planned_call(
        invocation_id,
        request_fingerprint,
        ToolResultEnvelope(
            call_id=invocation_id,
            run_id=run_id,
            task_attempt_id="root-task:1",
            tool_id="git.commit",
            tool_version="1.0",
            status=CallStatus.FAILED,
            started_at=now,
            completed_at=now,
            retryable=True,
            reason="dispatch was proven to have no effect",
        ),
        EffectSettlement.ABSENT,
    )
    assert repository.recover_interrupted(run_id) == ("root-task",)
    assert repository.run_state(run_id) == RunState.RUNNING.value
    assert repository.claim_task(run_id, "root-task") == 2


def test_interrupted_call_without_declared_effect_is_released_for_retry(tmp_path: Path) -> None:
    repository, run_id = _repository(tmp_path)
    repository.claim_task(run_id, "root-task")
    invocation_id = str(uuid4())
    request_fingerprint = "a" * 64
    repository.reserve_planned_call(
        invocation_id=invocation_id,
        run_id=run_id,
        task_attempt_id="root-task:1",
        planned_call_id="read-evidence",
        request_fingerprint=request_fingerprint,
        tool_id="repository.read_file",
        tool_version="1.0",
        effect_class="read",
        declared_effects=(),
    )
    repository.mark_planned_call_dispatching(invocation_id, request_fingerprint)

    assert repository.recover_interrupted(run_id) == ("root-task",)
    assert repository.unresolved_planned_effects(run_id) == ()
    assert (
        repository.reserve_planned_call(
            invocation_id=invocation_id,
            run_id=run_id,
            task_attempt_id="root-task:1",
            planned_call_id="read-evidence",
            request_fingerprint=request_fingerprint,
            tool_id="repository.read_file",
            tool_version="1.0",
            effect_class="read",
            declared_effects=(),
        )
        is None
    )


def test_bounded_loop_uses_the_predicate_dsl_for_its_exit() -> None:
    loop = BoundedPredicateLoop({"eq": ["review.accepted", True]}, maximum_iterations=2)
    assert list(loop) == [1, 2]
    assert loop.is_complete({"review": {"accepted": False}}) is False
    assert loop.is_complete({"review": {"accepted": True}}) is True


def test_predicate_dsl_is_bounded_and_never_evaluates_python() -> None:
    evaluator = PredicateEvaluator(PredicateLimits(max_depth=3, max_nodes=8))
    results = {"tasks": {"qa": {"accepted": True, "score": 9}}, "tags": ["secure"]}
    assert evaluator.evaluate(
        {
            "all": [
                {"eq": ["tasks.qa.accepted", True]},
                {"ge": ["tasks.qa.score", 8]},
                {"in": ["tags.0", ["secure", "quality"]]},
            ]
        },
        results,
    )
    evaluator.validate_loop(maximum_iterations=5)
    with pytest.raises(MishkanError):
        evaluator.evaluate({"eval": ["__import__('os').system('true')"]}, results)
    with pytest.raises(MishkanError):
        evaluator.validate_loop(maximum_iterations=0)


@pytest.mark.parametrize(
    ("predicate", "expected"),
    [
        ({"any": [{"eq": ["value", 1]}, {"eq": ["value", 2]}]}, True),
        ({"not": {"exists": "missing"}}, True),
        ({"exists": "items.0.name"}, True),
        ({"ne": ["value", 1]}, True),
        ({"lt": ["value", 3]}, True),
        ({"le": ["value", 2]}, True),
        ({"gt": ["value", 1]}, True),
        ({"contains": ["items.0.name", "ish"]}, True),
        ({"eq": ["missing", None]}, False),
    ],
)
def test_predicate_dsl_supported_operators(predicate: dict[str, object], expected: bool) -> None:
    evaluator = PredicateEvaluator()
    assert evaluator.evaluate(predicate, {"value": 2, "items": [{"name": "mishkan"}]}) is expected


@pytest.mark.parametrize(
    "predicate",
    [
        {},
        {"all": []},
        {"all": ["invalid"]},
        {"not": []},
        {"exists": []},
        {"eq": ["value"]},
        {"lt": ["value", object()]},
    ],
)
def test_predicate_dsl_rejects_malformed_or_incompatible_inputs(
    predicate: dict[str, object],
) -> None:
    evaluator = PredicateEvaluator(PredicateLimits(max_depth=1, max_nodes=2))
    with pytest.raises(MishkanError):
        evaluator.evaluate(predicate, {"value": 1})

    with pytest.raises(MishkanError):
        evaluator.evaluate({"not": {"not": {"eq": ["value", 1]}}}, {"value": 1})

    with pytest.raises(MishkanError):
        evaluator.evaluate({"exists": ".invalid"}, {"value": 1})
