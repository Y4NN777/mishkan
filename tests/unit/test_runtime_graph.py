from __future__ import annotations

from pathlib import Path

import pytest

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.persistence import LocalRunRepository, SchemaManager
from mishkan.planning.models import (
    AcceptedPlan,
    InitializationResult,
    PlanTask,
    ReviewDecision,
)
from mishkan.repository.models import DiscoverySnapshot, RepositoryBinding
from mishkan.runtime import PredicateEvaluator, PredicateLimits, TaskState


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
    repository.accept_plan(run.run_id, _plan())
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
    assert len(repository.outbox_events()) == count

    repository.cancel_run(run_id)
    assert repository.task_states(run_id)["dependent-task"] == TaskState.CANCELLED.value
    with pytest.raises(MishkanError):
        repository.claim_task(run_id, "dependent-task")


def test_interrupted_task_requires_effect_reconciliation_before_retry(tmp_path: Path) -> None:
    repository, run_id = _repository(tmp_path)
    repository.claim_task(run_id, "root-task")
    with pytest.raises(MishkanError) as blocked:
        repository.recover_interrupted(run_id, uncertain_effects=("change:1",))
    assert blocked.value.envelope.details["automatic_retry"] is False
    assert repository.recover_interrupted(run_id) == ("root-task",)
    assert repository.claim_task(run_id, "root-task") == 2


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
