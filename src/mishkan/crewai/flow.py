"""CrewAI Flow coordinating plan proposal, acceptance, execution, and resume."""

from __future__ import annotations

from typing import ClassVar

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel, Field

from mishkan.crewai.coordinator import CrewAIInitializationCoordinator
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.organization.models import OrganizationDefinition, OutcomeDefinition
from mishkan.persistence import LocalRunRepository
from mishkan.planning.models import (
    AcceptedPlan,
    InitializationReport,
    InitializationResult,
    ReviewDecision,
)
from mishkan.planning.result_validator import ResultValidator
from mishkan.planning.validator import PlanValidator
from mishkan.repository.models import DiscoverySnapshot


class InitializationFlowState(BaseModel):
    run_id: str
    objective: str
    discovery: DiscoverySnapshot
    resumed: bool = False
    accepted_plan: AcceptedPlan | None = None
    accepted_results: list[InitializationResult] = Field(default_factory=list)
    accepted_reviews: list[ReviewDecision] = Field(default_factory=list)


class CrewAIInitializationFlow(Flow[InitializationFlowState]):
    """Supported CrewAI Flow with MISHKAN-owned authoritative persistence."""

    # CrewAI's built-in flows use this hook to avoid an implicit LanceDB Memory.
    # MISHKAN owns memory and persistence explicitly; the contract test pins this boundary.
    _skip_auto_memory: ClassVar[bool] = True
    initial_state = InitializationFlowState

    def __init__(
        self,
        state: InitializationFlowState,
        coordinator: CrewAIInitializationCoordinator,
        repository: LocalRunRepository,
        organization: OrganizationDefinition,
        outcome: OutcomeDefinition,
        plan_validator: PlanValidator | None = None,
        *,
        tracing: bool,
    ) -> None:
        super().__init__(
            initial_state=state,
            tracing=tracing,
            suppress_flow_events=True,
        )
        self._coordinator = coordinator
        self._repository = repository
        self._organization = organization
        self._outcome = outcome
        self._plan_validator = plan_validator
        self._result_validator = ResultValidator()

    @start()
    def establish_plan(self) -> AcceptedPlan:
        if self.state.accepted_plan is None:
            if self._plan_validator is None:
                raise RuntimeError("a plan validator is required before plan proposal")
            validation_feedback: tuple[str, ...] = ()
            last_error: MishkanError | None = None
            attempts = self._coordinator.plan_validation_retries + 1
            for _attempt in range(attempts):
                candidate = self._coordinator.propose_plan(
                    self.state.discovery,
                    self.state.objective,
                    validation_feedback,
                )
                try:
                    accepted = self._plan_validator.accept(
                        candidate,
                        self.state.discovery,
                        self._organization,
                        self._outcome,
                    )
                    break
                except MishkanError as error:
                    if error.envelope.code is not ErrorCode.PLAN:
                        raise
                    last_error = error
                    raw_violations = error.envelope.details.get("violations", [])
                    validation_feedback = tuple(str(item) for item in raw_violations)
            else:
                if last_error is None:
                    raise RuntimeError("plan validation loop produced no result")
                raise last_error
            self._repository.accept_plan(self.state.run_id, accepted)
            self.state.accepted_plan = accepted
        plan = self.state.accepted_plan
        if not isinstance(plan, AcceptedPlan):
            raise RuntimeError("CrewAI Flow did not establish an accepted plan")
        return plan

    @listen(establish_plan)
    def execute_plan(self, plan: AcceptedPlan) -> InitializationReport:
        completed = {result.task_id for result in self.state.accepted_results}
        pending = {task.task_id: task for task in plan.tasks if task.task_id not in completed}
        while pending:
            ready = [
                task
                for task in plan.tasks
                if task.task_id in pending and set(task.depends_on).issubset(completed)
            ]
            for task in ready:
                review_feedback: ReviewDecision | None = None
                verified: InitializationResult | None = None
                accepted_review: ReviewDecision | None = None
                attempts = self._coordinator.review_retries + 1
                for _attempt in range(attempts):
                    proposed = self._coordinator.execute_task(
                        self.state.run_id,
                        plan,
                        self.state.discovery,
                        task,
                        review_feedback,
                    )
                    verified = self._result_validator.verify(
                        proposed,
                        task,
                        self.state.discovery,
                    )
                    proposed_review = self._coordinator.review_task(
                        self.state.run_id,
                        plan,
                        self.state.discovery,
                        task,
                        verified,
                    )
                    if proposed_review.verdict == "accepted":
                        accepted_review = self._result_validator.accept_review(
                            proposed_review,
                            verified,
                        )
                        break
                    review_feedback = proposed_review
                if verified is None or accepted_review is None:
                    if review_feedback is None or verified is None:
                        raise RuntimeError("review loop produced no result")
                    self._result_validator.accept_review(review_feedback, verified)
                    raise RuntimeError("rejected review was unexpectedly accepted")
                self._repository.accept_result(
                    self.state.run_id,
                    verified,
                    accepted_review,
                )
                self.state.accepted_results.append(verified)
                self.state.accepted_reviews.append(accepted_review)
                completed.add(task.task_id)
                pending.pop(task.task_id)
        return InitializationReport(
            run_id=self.state.run_id,
            repository_id=self.state.discovery.binding.repository_id,
            repository_revision=self.state.discovery.binding.base_revision,
            discovery_fingerprint=self.state.discovery.fingerprint,
            plan_fingerprint=plan.fingerprint,
            resumed=self.state.resumed,
            completed_task_ids=tuple(result.task_id for result in self.state.accepted_results),
            results=tuple(self.state.accepted_results),
            reviews=tuple(self.state.accepted_reviews),
        )
