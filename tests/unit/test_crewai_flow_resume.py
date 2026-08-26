from __future__ import annotations

from pathlib import Path

import pytest

from mishkan.crewai.flow import CrewAIInitializationFlow, InitializationFlowState
from mishkan.organization import load_initialization_definitions
from mishkan.persistence import LocalRunRepository
from mishkan.planning.models import (
    AcceptedPlan,
    InitializationResult,
    PlanTask,
    ReviewDecision,
)
from mishkan.repository.models import DiscoverySnapshot, RepositoryBinding


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
        objective="Initialize after a forced crash",
        outcome_id="mishkan.init",
        repository_revision="b" * 40,
        tasks=(
            PlanTask(
                task_id="inspect-overview",
                title="Inspect project overview",
                purpose="Ground the first finding in project evidence.",
                assigned_role="Repository_Investigator",
                tools=("repository.read_file",),
                evidence_paths=("README.md",),
            ),
            PlanTask(
                task_id="inspect-details",
                title="Inspect project details",
                purpose="Ground the second finding after the first.",
                depends_on=("inspect-overview",),
                assigned_role="Repository_Investigator",
                tools=("repository.read_file",),
                evidence_paths=("README.md",),
            ),
        ),
        fingerprint="d" * 64,
        discovery_fingerprint="c" * 64,
    )


class FakeCoordinator:
    plan_validation_retries = 0
    review_retries = 0

    def __init__(
        self,
        *,
        crash_on: str | None = None,
        rejected_reviews: int = 0,
        invalid_accepted_reviews: int = 0,
    ) -> None:
        self.crash_on = crash_on
        self.review_retries = rejected_reviews
        self.rejected_reviews = rejected_reviews
        self.invalid_accepted_reviews = invalid_accepted_reviews
        self.evidence_executed: list[str] = []
        self.executed: list[str] = []
        self.reviewed = 0
        self.awaiting_contract_feedback = False

    def execute_task_evidence(
        self,
        _run_id: str,
        _plan: AcceptedPlan,
        _discovery: DiscoverySnapshot,
        task: PlanTask,
    ) -> str:
        self.evidence_executed.append(task.task_id)
        return f'{{"task_id":"{task.task_id}","content":"evidence"}}'

    def execute_review_evidence(
        self,
        _run_id: str,
        _plan: AcceptedPlan,
        _discovery: DiscoverySnapshot,
        task: PlanTask,
    ) -> str:
        return f'{{"task_id":"{task.task_id}","content":"evidence"}}'

    def execute_task(
        self,
        _plan: AcceptedPlan,
        discovery: DiscoverySnapshot,
        task: PlanTask,
        _call_evidence: str,
        review_feedback: ReviewDecision | None = None,
    ) -> InitializationResult:
        self.executed.append(task.task_id)
        if task.task_id == self.crash_on:
            raise RuntimeError("forced crash")
        if self.executed.count(task.task_id) > 1:
            assert review_feedback is not None
        return InitializationResult(
            repository_revision=discovery.binding.base_revision,
            task_id=task.task_id,
            summary=f"Verified {task.task_id} from the README.",
            cited_paths=("README.md",),
            findings=(f"Finding for {task.task_id}.",),
        )

    def review_task(
        self,
        task: PlanTask,
        _result: InitializationResult,
        _review_evidence: str,
        contract_feedback: tuple[str, ...] = (),
    ) -> ReviewDecision:
        self.reviewed += 1
        if self.awaiting_contract_feedback:
            assert contract_feedback
            self.awaiting_contract_feedback = False
        if self.reviewed <= self.rejected_reviews:
            return ReviewDecision(
                task_id=task.task_id,
                verdict="rejected",
                summary="Independent evidence check requested another review.",
                checked_citations=("README.md",),
                issues=("Review retry fixture",),
            )
        if self.reviewed <= self.rejected_reviews + self.invalid_accepted_reviews:
            assert not contract_feedback
            self.awaiting_contract_feedback = True
            return ReviewDecision(
                task_id=task.task_id,
                verdict="accepted",
                summary="Review fixture omitted the required repository path.",
                checked_citations=("not-a-bound-path.md",),
            )
        return ReviewDecision(
            task_id=task.task_id,
            verdict="accepted",
            summary="Independent evidence check passed.",
            checked_citations=("README.md",),
        )


def _flow(
    state: InitializationFlowState,
    coordinator: FakeCoordinator,
    repository: LocalRunRepository,
) -> CrewAIInitializationFlow:
    organization, outcome = load_initialization_definitions()
    return CrewAIInitializationFlow(
        state,
        coordinator,  # type: ignore[arg-type]
        repository,
        organization,
        outcome,
        tracing=False,
    )


def test_flow_resumes_after_last_accepted_task(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("evidence", encoding="utf-8")
    discovery = _discovery(tmp_path)
    repository = LocalRunRepository(tmp_path / ".mishkan" / "mishkan.db")
    started = repository.start_or_resume(
        discovery,
        "Initialize after a forced crash",
        "mishkan.init",
    )
    plan = _plan()
    repository.accept_plan(started.run_id, plan)
    first_coordinator = FakeCoordinator(crash_on="inspect-details")
    first_state = InitializationFlowState(
        run_id=started.run_id,
        objective=plan.objective,
        discovery=discovery,
        accepted_plan=plan,
    )

    first_flow = _flow(first_state, first_coordinator, repository)
    with pytest.raises(RuntimeError, match="forced crash"):
        first_flow.execute_plan(first_flow.establish_plan())

    snapshot = repository.start_or_resume(discovery, plan.objective, plan.outcome_id)
    assert snapshot.completed_task_ids == {"inspect-overview"}
    resumed_coordinator = FakeCoordinator()
    resumed_state = InitializationFlowState(
        run_id=snapshot.run_id,
        objective=plan.objective,
        discovery=discovery,
        resumed=snapshot.resumed,
        accepted_plan=snapshot.plan,
        accepted_results=list(snapshot.results),
        accepted_reviews=list(snapshot.reviews),
    )

    resumed_flow = _flow(resumed_state, resumed_coordinator, repository)
    report = resumed_flow.execute_plan(resumed_flow.establish_plan())

    assert report.resumed is True
    assert resumed_coordinator.executed == ["inspect-details"]
    assert report.completed_task_ids == ("inspect-overview", "inspect-details")


def test_review_rejection_resynthesizes_without_reexecuting_evidence(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("evidence", encoding="utf-8")
    discovery = _discovery(tmp_path)
    repository = LocalRunRepository(tmp_path / ".mishkan" / "mishkan.db")
    started = repository.start_or_resume(
        discovery,
        "Initialize after a forced crash",
        "mishkan.init",
    )
    plan = _plan()
    repository.accept_plan(started.run_id, plan)
    coordinator = FakeCoordinator(rejected_reviews=1)
    state = InitializationFlowState(
        run_id=started.run_id,
        objective=plan.objective,
        discovery=discovery,
        accepted_plan=plan,
    )

    report = _flow(state, coordinator, repository).execute_plan(plan)

    assert coordinator.evidence_executed == ["inspect-overview", "inspect-details"]
    assert coordinator.executed == [
        "inspect-overview",
        "inspect-overview",
        "inspect-details",
    ]
    assert coordinator.reviewed == 3
    assert report.completed_task_ids == ("inspect-overview", "inspect-details")


def test_invalid_accepted_review_is_corrected_without_resynthesizing_task(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("evidence", encoding="utf-8")
    discovery = _discovery(tmp_path)
    repository = LocalRunRepository(tmp_path / ".mishkan" / "mishkan.db")
    started = repository.start_or_resume(
        discovery,
        "Initialize after a forced crash",
        "mishkan.init",
    )
    plan = _plan()
    repository.accept_plan(started.run_id, plan)
    coordinator = FakeCoordinator(invalid_accepted_reviews=1)
    coordinator.review_retries = 1
    state = InitializationFlowState(
        run_id=started.run_id,
        objective=plan.objective,
        discovery=discovery,
        accepted_plan=plan,
    )

    report = _flow(state, coordinator, repository).execute_plan(plan)

    assert coordinator.evidence_executed == ["inspect-overview", "inspect-details"]
    assert coordinator.executed == ["inspect-overview", "inspect-details"]
    assert coordinator.reviewed == 3
    assert report.completed_task_ids == ("inspect-overview", "inspect-details")
