from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from mishkan.conversations import (
    EscalationOption,
    EscalationState,
    ExecutiveRecommendation,
    MissionEscalation,
)
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.missions import (
    CrewAssignmentKind,
    EnvironmentReadinessState,
    MissionEnvironmentReadiness,
    MissionOrigin,
    MissionOriginKind,
    MissionRecord,
    MissionResourceLimit,
    MissionRunAcceptance,
    MissionRunBinding,
    MissionRunReport,
    MissionState,
    MissionTaskAssignment,
    MissionTaskClaimRequest,
    MissionTaskClaimService,
    MissionTaskEnvironmentReadiness,
    MissionTaskGateState,
)
from mishkan.planning import PlanExecutionContext, PlanTask
from mishkan.runtime import RunState, TaskState


@dataclass
class _Missions:
    mission_record: MissionRecord
    assignments_: tuple[MissionTaskAssignment, ...]
    bindings_: tuple[MissionRunBinding, ...] = ()
    reports_: tuple[MissionRunReport, ...] = ()

    def mission(self, mission_id: str) -> MissionRecord:
        assert mission_id == str(self.mission_record.mission_id)
        return self.mission_record

    def assignments(
        self, mission_id: str, *, limit: int = 1_000
    ) -> tuple[MissionTaskAssignment, ...]:
        assert mission_id == str(self.mission_record.mission_id)
        assert limit == 1_000
        return self.assignments_

    def run_reports(self, mission_id: str, *, limit: int = 1_000) -> tuple[MissionRunReport, ...]:
        assert mission_id == str(self.mission_record.mission_id)
        assert limit == 1_000
        return self.reports_

    def run_bindings(self, mission_id: str, *, limit: int = 1_000) -> tuple[MissionRunBinding, ...]:
        assert mission_id == str(self.mission_record.mission_id)
        assert limit == 1_000
        return self.bindings_


@dataclass
class _Conversations:
    escalations_: tuple[MissionEscalation, ...] = ()

    def escalations(
        self,
        mission_id: str,
        *,
        state: EscalationState | None = None,
        limit: int = 100,
    ) -> tuple[MissionEscalation, ...]:
        assert state is None
        assert limit == 1_000
        return self.escalations_

    def interventions(self, mission_id: str, *, limit: int = 100) -> tuple[object, ...]:
        assert limit == 1_000
        return ()


@dataclass
class _Readiness:
    projection: MissionEnvironmentReadiness

    def inspect(self, mission_id: str) -> MissionEnvironmentReadiness:
        assert mission_id == str(self.projection.mission_id)
        return self.projection


@dataclass
class _Runs:
    states: dict[str, dict[str, str]]
    contracts: dict[tuple[str, str], PlanTask]
    run_states: dict[str, str] = field(default_factory=lambda: {"run-1": RunState.RUNNING.value})
    claims: list[tuple[str, str]] = field(default_factory=list)

    def task_states(self, run_id: str) -> dict[str, str]:
        return self.states[run_id]

    def task_contract(self, run_id: str, task_id: str) -> PlanTask:
        return self.contracts[(run_id, task_id)]

    def run_state(self, run_id: str) -> str:
        return self.run_states[run_id]

    def claim_task(self, run_id: str, task_id: str) -> int:
        self.claims.append((run_id, task_id))
        self.states[run_id][task_id] = TaskState.EXECUTING.value
        return 1

    def task_count(self, run_id: str) -> int:
        return len(self.states[run_id])


def _binding(
    assignment: MissionTaskAssignment,
    *,
    acceptance: MissionRunAcceptance = MissionRunAcceptance.PENDING,
) -> MissionRunBinding:
    references: dict[str, object] = {}
    if acceptance is MissionRunAcceptance.ACCEPTED:
        references = {
            "result_references": (
                f"run-result:{assignment.execution_run_id}:{assignment.execution_task_id}",
            ),
            "acceptance_references": (
                f"run-acceptance:{assignment.execution_run_id}:{assignment.execution_task_id}",
            ),
        }
    return MissionRunBinding(
        mission_id=assignment.mission_id,
        binding_key=assignment.task_id,
        mission_task_id=assignment.task_id,
        assignment_id=assignment.assignment_id,
        assignment_revision=assignment.assignment_revision,
        run_id=assignment.execution_run_id or "missing-run",
        execution_task_id=assignment.execution_task_id or "missing-task",
        plan_fingerprint="a" * 64,
        execution_context=PlanExecutionContext(
            kind="repository",
            context_id="b" * 64,
            revision="c" * 40,
            repository_id="b" * 64,
            repository_revision="c" * 40,
        ),
        authority_scope=assignment.authority_scope,
        path_scopes=assignment.path_scopes,
        acceptance=acceptance,
        recorded_by="Mission_Lead",
        **references,
    )


def _fixture(
    *, environment_ready: bool
) -> tuple[
    MissionTaskClaimService,
    MissionRecord,
    MissionTaskAssignment,
    _Runs,
    _Conversations,
]:
    mission = MissionRecord(
        revision=7,
        state=MissionState.ACTIVE,
        origin=MissionOrigin(
            kind=MissionOriginKind.CEO,
            actor_id="CEO",
            objective="Execute only work whose complete gate is currently satisfied",
        ),
        organization_id="mishkan",
        organization_version="1",
        current_brief_version=1,
        current_crew_version=1,
        current_environment_plan_version=1,
    )
    assignment = MissionTaskAssignment(
        mission_id=mission.mission_id,
        crew_version=1,
        task_id="build-api",
        accountable_owner="Backend_Engineer",
        assignment_kind=CrewAssignmentKind.PRODUCTION,
        expected_result="A tested API change",
        completion_criteria=("independent review accepted",),
        execution_run_id="run-1",
        execution_task_id="build-api",
        environment_context_ids=("repository:api",),
        authority_scope=("repository:api",),
        exact_tools=("process.exec",),
        path_scopes=("repository:api",),
        limits=(MissionResourceLimit(name="wall_time", value=600, unit="seconds"),),
        required_evidence=("test-results",),
    )
    task_readiness = MissionTaskEnvironmentReadiness(
        task_id=assignment.task_id,
        assignment_revision=assignment.assignment_revision,
        state=(
            EnvironmentReadinessState.ELIGIBLE
            if environment_ready
            else EnvironmentReadinessState.AWAITING_VERIFICATION
        ),
        environment_ready=environment_ready,
        contexts=(),
        blockers=() if environment_ready else ("binding has no verification",),
    )
    readiness = MissionEnvironmentReadiness(
        mission_id=mission.mission_id,
        mission_revision=mission.revision,
        environment_plan_version=1,
        tasks=(task_readiness,),
        ready_task_ids=(assignment.task_id,) if environment_ready else (),
        blocked_task_ids=() if environment_ready else (assignment.task_id,),
    )
    runs = _Runs(
        {"run-1": {assignment.task_id: TaskState.ELIGIBLE.value}},
        {
            ("run-1", assignment.task_id): PlanTask(
                task_id=assignment.task_id,
                title="Build the API",
                purpose=assignment.expected_result,
                assigned_role=assignment.accountable_owner,
                tools=assignment.exact_tools,
                evidence_paths=("README.md",),
            )
        },
    )
    conversations = _Conversations()
    service = MissionTaskClaimService(
        _Missions(mission, (assignment,), (_binding(assignment),)),
        conversations,  # type: ignore[arg-type]
        _Readiness(readiness),  # type: ignore[arg-type]
        runs,
    )
    return service, mission, assignment, runs, conversations


def test_claim_refuses_generated_but_unverified_environment() -> None:
    service, mission, assignment, runs, _conversations = _fixture(environment_ready=False)

    eligibility = service.inspect(str(mission.mission_id), assignment.task_id)
    with pytest.raises(MishkanError) as blocked:
        service.claim(
            MissionTaskClaimRequest(
                mission_id=mission.mission_id,
                mission_revision=mission.revision,
                task_id=assignment.task_id,
                assignment_revision=assignment.assignment_revision,
            )
        )

    assert eligibility.state is MissionTaskGateState.BLOCKED
    assert "binding has no verification" in eligibility.blockers
    assert blocked.value.envelope.code is ErrorCode.MISSION
    assert runs.claims == []


def test_claim_refuses_assignment_from_an_obsolete_crew_revision() -> None:
    service, mission, assignment, runs, _conversations = _fixture(environment_ready=True)
    service._missions.assignments_ = (  # type: ignore[attr-defined]
        assignment.model_copy(update={"crew_version": 2}),
    )

    eligibility = service.inspect(str(mission.mission_id), assignment.task_id)

    assert not eligibility.eligible
    assert "current Mission Crew" in " ".join(eligibility.blockers)
    assert runs.claims == []


def test_claim_starts_exact_bound_run_task_after_all_gates_pass() -> None:
    service, mission, assignment, runs, _conversations = _fixture(environment_ready=True)

    claim = service.claim(
        MissionTaskClaimRequest(
            mission_id=mission.mission_id,
            mission_revision=mission.revision,
            task_id=assignment.task_id,
            assignment_revision=assignment.assignment_revision,
        )
    )

    assert claim.execution_run_id == "run-1"
    assert claim.execution_task_id == "build-api"
    assert claim.attempt == 1
    assert runs.claims == [("run-1", "build-api")]


def test_claim_refuses_assignment_without_its_durable_mission_run_binding() -> None:
    service, mission, assignment, runs, _conversations = _fixture(environment_ready=True)
    service._missions.bindings_ = ()  # type: ignore[attr-defined]

    eligibility = service.inspect(str(mission.mission_id), assignment.task_id)

    assert eligibility.state is MissionTaskGateState.BLOCKED
    assert "no exact durable mission run binding" in " ".join(eligibility.blockers)
    assert runs.claims == []


def test_claim_refuses_binding_from_an_obsolete_assignment_revision() -> None:
    service, mission, assignment, runs, _conversations = _fixture(environment_ready=True)
    revised = assignment.model_copy(update={"assignment_id": uuid4(), "assignment_revision": 2})
    service._missions.assignments_ = (revised,)  # type: ignore[attr-defined]

    eligibility = service.inspect(str(mission.mission_id), revised.task_id)

    assert eligibility.state is MissionTaskGateState.BLOCKED
    assert "assignment revision" in " ".join(eligibility.blockers)
    assert runs.claims == []


def test_open_escalation_pauses_only_matching_task_scope() -> None:
    service, mission, assignment, runs, conversations = _fixture(environment_ready=True)
    independent_assignment = assignment.model_copy(
        update={
            "task_id": "write-docs",
            "execution_task_id": "write-docs",
            "environment_context_ids": ("repository:docs",),
            "authority_scope": ("repository:docs",),
        }
    )
    runs.states["run-1"][independent_assignment.task_id] = TaskState.ELIGIBLE.value
    runs.contracts[("run-1", independent_assignment.task_id)] = PlanTask(
        task_id=independent_assignment.task_id,
        title="Write the documentation",
        purpose=independent_assignment.expected_result,
        assigned_role=independent_assignment.accountable_owner,
        tools=independent_assignment.exact_tools,
        evidence_paths=("README.md",),
    )
    conversations.escalations_ = (
        MissionEscalation(
            mission_id=mission.mission_id,
            conversation_id=uuid4(),
            state=EscalationState.OPEN,
            raised_by="CTO",
            blocked_scope=(f"task:{assignment.task_id}",),
            decision_required="Choose the bounded compatibility option",
            reason="PM and CTO disagree on one task constraint",
            options=(
                EscalationOption(
                    option_id="a",
                    description="Keep the constraint",
                    consequences=("delivery is delayed",),
                    risks=("schedule risk",),
                ),
                EscalationOption(
                    option_id="b",
                    description="Accept the exception",
                    consequences=("delivery continues",),
                    risks=("compatibility risk",),
                ),
            ),
            recommendations=(
                ExecutiveRecommendation(
                    identity_id="CTO",
                    recommended_option_id="a",
                    rationale="Preserve the verified platform constraint",
                    evidence_references=("evidence:compatibility",),
                ),
            ),
            uncertainty=("downstream consumer timing",),
            independent_work_continuing=("task:docs",),
            evidence_references=("evidence:compatibility",),
        ),
    )
    independent_readiness = MissionTaskEnvironmentReadiness(
        task_id=independent_assignment.task_id,
        assignment_revision=independent_assignment.assignment_revision,
        state=EnvironmentReadinessState.ELIGIBLE,
        environment_ready=True,
        contexts=(),
        blockers=(),
    )
    readiness = MissionEnvironmentReadiness(
        mission_id=mission.mission_id,
        mission_revision=mission.revision,
        environment_plan_version=1,
        tasks=(
            service.inspect(str(mission.mission_id), assignment.task_id).environment,
            independent_readiness,
        ),
        ready_task_ids=(assignment.task_id, independent_assignment.task_id),
        blocked_task_ids=(),
    )
    service = MissionTaskClaimService(
        _Missions(
            mission,
            (assignment, independent_assignment),
            (_binding(assignment), _binding(independent_assignment)),
        ),
        conversations,  # type: ignore[arg-type]
        _Readiness(readiness),  # type: ignore[arg-type]
        runs,
    )

    blocked = service.inspect(str(mission.mission_id), assignment.task_id)
    independent = service.inspect(str(mission.mission_id), independent_assignment.task_id)

    assert blocked.state is MissionTaskGateState.PAUSED
    assert blocked.blocking_escalation_ids == (conversations.escalations_[0].escalation_id,)
    assert independent.state is MissionTaskGateState.ELIGIBLE
    assert independent.blocking_escalation_ids == ()
    assert runs.claims == []


@pytest.mark.parametrize(
    ("contract_update", "blocker"),
    (
        (
            {"assigned_role": "Another_Engineer"},
            "bound run task owner differs from the mission assignment",
        ),
        (
            {"tools": ("file.read",)},
            "bound run task tools differ from the mission assignment",
        ),
    ),
)
def test_claim_refuses_drift_between_mission_assignment_and_run_contract(
    contract_update: dict[str, object],
    blocker: str,
) -> None:
    service, mission, assignment, runs, _conversations = _fixture(environment_ready=True)
    key = ("run-1", assignment.task_id)
    runs.contracts[key] = runs.contracts[key].model_copy(update=contract_update)

    eligibility = service.inspect(str(mission.mission_id), assignment.task_id)

    assert eligibility.state is MissionTaskGateState.BLOCKED
    assert blocker in eligibility.blockers
    assert runs.claims == []


def test_cross_run_mission_dependency_must_be_durably_accepted() -> None:
    service, mission, assignment, runs, conversations = _fixture(environment_ready=True)
    dependency = assignment.model_copy(
        update={
            "assignment_id": uuid4(),
            "task_id": "prepare-contract",
            "execution_run_id": "run-2",
            "execution_task_id": "prepare-contract",
            "environment_context_ids": (),
        }
    )
    dependent = assignment.model_copy(update={"dependencies": (dependency.task_id,)})
    environment = service.inspect(str(mission.mission_id), assignment.task_id).environment
    runs.states["run-2"] = {dependency.task_id: TaskState.EXECUTING.value}
    runs.contracts[("run-2", dependency.task_id)] = PlanTask(
        task_id=dependency.task_id,
        title="Prepare the contract",
        purpose=dependency.expected_result,
        assigned_role=dependency.accountable_owner,
        tools=dependency.exact_tools,
        evidence_paths=("README.md",),
    )
    readiness = MissionEnvironmentReadiness(
        mission_id=mission.mission_id,
        mission_revision=mission.revision,
        environment_plan_version=1,
        tasks=(environment,),
        ready_task_ids=(dependent.task_id,),
        blocked_task_ids=(),
    )
    service = MissionTaskClaimService(
        _Missions(
            mission,
            (dependency, dependent),
            (_binding(dependency), _binding(dependent)),
        ),
        conversations,  # type: ignore[arg-type]
        _Readiness(readiness),  # type: ignore[arg-type]
        runs,
    )

    blocked = service.inspect(str(mission.mission_id), dependent.task_id)
    runs.states["run-2"][dependency.task_id] = TaskState.ACCEPTED.value
    eligible = service.inspect(str(mission.mission_id), dependent.task_id)

    assert blocked.state is MissionTaskGateState.BLOCKED
    assert "mission task dependency prepare-contract is not durably accepted" in blocked.blockers
    assert eligible.state is MissionTaskGateState.ELIGIBLE


def test_completion_requires_current_environment_and_durable_run_acceptance() -> None:
    service, mission, assignment, runs, conversations = _fixture(environment_ready=True)
    evaluating = mission.model_copy(update={"state": MissionState.EVALUATING})
    environment = service.inspect(str(mission.mission_id), assignment.task_id).environment
    readiness = MissionEnvironmentReadiness(
        mission_id=mission.mission_id,
        mission_revision=mission.revision,
        environment_plan_version=1,
        tasks=(environment,),
        ready_task_ids=(assignment.task_id,),
        blocked_task_ids=(),
    )
    service = MissionTaskClaimService(
        _Missions(
            evaluating,
            (assignment,),
            (_binding(assignment, acceptance=MissionRunAcceptance.ACCEPTED),),
        ),
        conversations,  # type: ignore[arg-type]
        _Readiness(readiness),  # type: ignore[arg-type]
        runs,
    )

    before = service.inspect_completion(str(mission.mission_id))
    runs.states["run-1"][assignment.task_id] = TaskState.ACCEPTED.value
    runs.run_states["run-1"] = RunState.COMPLETED.value
    ready = service.inspect_completion(str(mission.mission_id))

    assert not before.ready
    assert "task build-api is not durably accepted" in before.blockers
    assert ready.ready
    assert ready.tasks[0].accepted
