"""Mission-scoped task claim gate over durable run execution."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import require_aware, utc_now
from mishkan.missions.assignment_graph import MissionAssignmentGraphValidator
from mishkan.missions.models import (
    CrewAssignmentKind,
    MissionRecord,
    MissionRunAcceptance,
    MissionRunBinding,
    MissionRunReport,
    MissionState,
    MissionTaskAssignment,
)
from mishkan.missions.readiness import (
    MissionEnvironmentReadinessService,
    MissionTaskEnvironmentReadiness,
)
from mishkan.planning import PlanTask
from mishkan.runtime import RunState, TaskState

if TYPE_CHECKING:
    from mishkan.conversations.models import (
        EscalationState,
        MissionEscalation,
        MissionIntervention,
    )


class MissionExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MissionTaskGateState(StrEnum):
    ELIGIBLE = "eligible"
    PAUSED = "paused"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class MissionTaskClaimRequest(MissionExecutionModel):
    schema_version: Literal["1.0"] = "1.0"
    mission_id: UUID
    mission_revision: int = Field(ge=1)
    task_id: str = Field(min_length=1, max_length=256)
    assignment_revision: int = Field(ge=1)


class MissionTaskEligibility(MissionExecutionModel):
    schema_version: Literal["1.0"] = "1.0"
    mission_id: UUID
    mission_revision: int = Field(ge=1)
    task_id: str = Field(min_length=1, max_length=256)
    assignment_revision: int = Field(ge=1)
    state: MissionTaskGateState
    eligible: bool
    execution_run_id: str | None = None
    execution_task_id: str | None = None
    run_task_state: str | None = None
    environment: MissionTaskEnvironmentReadiness
    blocking_escalation_ids: tuple[UUID, ...]
    blockers: tuple[str, ...]


class MissionTaskClaim(MissionExecutionModel):
    schema_version: Literal["1.0"] = "1.0"
    mission_id: UUID
    mission_revision: int = Field(ge=1)
    task_id: str = Field(min_length=1, max_length=256)
    assignment_revision: int = Field(ge=1)
    execution_run_id: str = Field(min_length=1, max_length=256)
    execution_task_id: str = Field(min_length=1, max_length=256)
    attempt: int = Field(ge=1)
    environment_plan_version: int | None = Field(default=None, ge=1)
    claimed_at: datetime = Field(default_factory=utc_now)

    @field_validator("claimed_at")
    @classmethod
    def claimed_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class MissionTaskAcceptanceStatus(MissionExecutionModel):
    task_id: str = Field(min_length=1, max_length=256)
    assignment_revision: int = Field(ge=1)
    assignment_kind: CrewAssignmentKind
    execution_run_id: str | None = None
    execution_task_id: str | None = None
    run_state: str | None = None
    run_task_state: str | None = None
    mission_run_acceptance: MissionRunAcceptance | None = None
    environment_ready: bool
    accepted: bool


class MissionCompletionReadiness(MissionExecutionModel):
    schema_version: Literal["1.0"] = "1.0"
    mission_id: UUID
    mission_revision: int = Field(ge=1)
    mission_state: MissionState
    ready: bool
    tasks: tuple[MissionTaskAcceptanceStatus, ...]
    blockers: tuple[str, ...]


class MissionExecutionRepository(Protocol):
    def mission(self, mission_id: str) -> MissionRecord: ...

    def assignments(
        self, mission_id: str, *, limit: int = 1_000
    ) -> tuple[MissionTaskAssignment, ...]: ...

    def run_reports(
        self, mission_id: str, *, limit: int = 1_000
    ) -> tuple[MissionRunReport, ...]: ...

    def run_bindings(
        self, mission_id: str, *, limit: int = 1_000
    ) -> tuple[MissionRunBinding, ...]: ...


class MissionConversationLookup(Protocol):
    def escalations(
        self,
        mission_id: str,
        *,
        state: EscalationState | None = None,
        limit: int = 100,
    ) -> tuple[MissionEscalation, ...]: ...

    def interventions(
        self, mission_id: str, *, limit: int = 100
    ) -> tuple[MissionIntervention, ...]: ...


class RunTaskAuthority(Protocol):
    def task_states(self, run_id: str) -> dict[str, str]: ...

    def task_contract(self, run_id: str, task_id: str) -> PlanTask: ...

    def run_state(self, run_id: str) -> str: ...

    def claim_task(self, run_id: str, task_id: str) -> int: ...

    def task_count(self, run_id: str) -> int: ...


class MissionTaskClaimService:
    """Revalidate every mission-level gate immediately before a durable run claim."""

    _RUNNABLE_MISSION_STATES = frozenset(
        {MissionState.ACTIVE, MissionState.EVALUATING, MissionState.REMEDIATING}
    )

    def __init__(
        self,
        missions: MissionExecutionRepository,
        conversations: MissionConversationLookup,
        readiness: MissionEnvironmentReadinessService,
        runs: RunTaskAuthority,
    ) -> None:
        self._missions = missions
        self._conversations = conversations
        self._readiness = readiness
        self._runs = runs

    def inspect(self, mission_id: str, task_id: str) -> MissionTaskEligibility:
        mission = self._missions.mission(mission_id)
        assignments = self._latest_assignments(mission_id)
        assignment = assignments.get(task_id)
        if assignment is None:
            raise MishkanError(ErrorCode.MISSION, "mission task assignment does not exist")
        environment = next(
            (item for item in self._readiness.inspect(mission_id).tasks if item.task_id == task_id),
            None,
        )
        if environment is None:
            raise MishkanError(
                ErrorCode.MISSION,
                "mission task has no environment-readiness projection",
            )
        blockers: list[str] = []
        state = MissionTaskGateState.ELIGIBLE
        run_binding = self._binding_for_assignment(mission_id, assignment)
        if mission.state is MissionState.CANCELLED:
            state = MissionTaskGateState.CANCELLED
            blockers.append("mission is cancelled")
        elif mission.state not in self._RUNNABLE_MISSION_STATES:
            state = (
                MissionTaskGateState.PAUSED
                if mission.state is MissionState.PAUSED
                else MissionTaskGateState.BLOCKED
            )
            blockers.append(f"mission state {mission.state.value} does not release task execution")
        if assignment.crew_version != mission.current_crew_version:
            state = MissionTaskGateState.BLOCKED
            blockers.append("task assignment does not reference the current Mission Crew")

        scoped = self._latest_scoped_intervention(mission_id, assignment)
        if scoped is not None and scoped.kind.value in {"suspend", "stop"}:
            state = (
                MissionTaskGateState.PAUSED
                if scoped.kind.value == "suspend"
                else MissionTaskGateState.CANCELLED
            )
            blockers.append(f"task is governed by CEO intervention {scoped.intervention_id}")

        escalations = tuple(
            item
            for item in self._conversations.escalations(
                mission_id,
                limit=1_000,
            )
            if item.state.value == "open"
            if self._scope_matches(item.blocked_scope, assignment)
        )
        if escalations:
            state = MissionTaskGateState.PAUSED
            blockers.append("task scope is blocked by an unresolved executive escalation")
        if not environment.environment_ready:
            state = MissionTaskGateState.BLOCKED
            blockers.extend(environment.blockers)

        run_state: str | None = None
        if assignment.execution_run_id is None or assignment.execution_task_id is None:
            state = MissionTaskGateState.BLOCKED
            blockers.append("task has no explicit durable run-task binding")
        else:
            if run_binding is None:
                state = MissionTaskGateState.BLOCKED
                blockers.append(
                    "task has no exact durable mission run binding for its assignment revision"
                )
            elif run_binding.acceptance is not MissionRunAcceptance.PENDING:
                state = MissionTaskGateState.BLOCKED
                blockers.append(f"mission run binding is already {run_binding.acceptance.value}")
            try:
                contract = self._runs.task_contract(
                    assignment.execution_run_id,
                    assignment.execution_task_id,
                )
                run_state = self._runs.task_states(assignment.execution_run_id).get(
                    assignment.execution_task_id
                )
            except MishkanError as error:
                if error.envelope.code is not ErrorCode.RUN_INTERRUPTED:
                    raise
                contract = None
                state = MissionTaskGateState.BLOCKED
                blockers.append("bound durable run task does not exist")
            if contract is not None:
                if contract.assigned_role != assignment.accountable_owner:
                    state = MissionTaskGateState.BLOCKED
                    blockers.append("bound run task owner differs from the mission assignment")
                if set(contract.tools) != set(assignment.exact_tools):
                    state = MissionTaskGateState.BLOCKED
                    blockers.append("bound run task tools differ from the mission assignment")
                same_run_dependencies: set[str] = set()
                for dependency_id in assignment.dependencies:
                    dependency = assignments.get(dependency_id)
                    if dependency is None:
                        state = MissionTaskGateState.BLOCKED
                        blockers.append(f"mission task dependency {dependency_id} is not assigned")
                        continue
                    if dependency.execution_run_id is None or dependency.execution_task_id is None:
                        state = MissionTaskGateState.BLOCKED
                        blockers.append(
                            f"mission task dependency {dependency_id} has no durable run binding"
                        )
                        continue
                    dependency_state = self._runs.task_states(dependency.execution_run_id).get(
                        dependency.execution_task_id
                    )
                    if dependency_state != TaskState.ACCEPTED.value:
                        state = MissionTaskGateState.BLOCKED
                        blockers.append(
                            f"mission task dependency {dependency_id} is not durably accepted"
                        )
                    if dependency.execution_run_id == assignment.execution_run_id:
                        same_run_dependencies.add(dependency.execution_task_id)
                if set(contract.depends_on) != same_run_dependencies:
                    state = MissionTaskGateState.BLOCKED
                    blockers.append(
                        "bound run dependency graph differs from the mission assignment"
                    )
            if run_state != TaskState.ELIGIBLE.value:
                state = MissionTaskGateState.BLOCKED
                blockers.append(
                    "bound run task is not eligible after durable dependency evaluation"
                )

        return MissionTaskEligibility(
            mission_id=mission.mission_id,
            mission_revision=mission.revision,
            task_id=assignment.task_id,
            assignment_revision=assignment.assignment_revision,
            state=state,
            eligible=state is MissionTaskGateState.ELIGIBLE and not blockers,
            execution_run_id=assignment.execution_run_id,
            execution_task_id=assignment.execution_task_id,
            run_task_state=run_state,
            environment=environment,
            blocking_escalation_ids=tuple(item.escalation_id for item in escalations),
            blockers=tuple(dict.fromkeys(blockers)),
        )

    def claim(self, request: MissionTaskClaimRequest) -> MissionTaskClaim:
        eligibility = self.inspect(str(request.mission_id), request.task_id)
        if eligibility.mission_revision != request.mission_revision:
            raise MishkanError(
                ErrorCode.REVISION_MISMATCH,
                "mission changed before task claim",
                details={
                    "expected": request.mission_revision,
                    "current": eligibility.mission_revision,
                },
            )
        if eligibility.assignment_revision != request.assignment_revision:
            raise MishkanError(
                ErrorCode.REVISION_MISMATCH,
                "mission task assignment changed before claim",
            )
        if not eligibility.eligible:
            raise MishkanError(
                ErrorCode.MISSION,
                "mission task is not eligible for execution",
                details={"state": eligibility.state.value, "blockers": eligibility.blockers},
            )
        assert eligibility.execution_run_id is not None
        assert eligibility.execution_task_id is not None
        attempt = self._runs.claim_task(
            eligibility.execution_run_id,
            eligibility.execution_task_id,
        )
        return MissionTaskClaim(
            mission_id=eligibility.mission_id,
            mission_revision=eligibility.mission_revision,
            task_id=eligibility.task_id,
            assignment_revision=eligibility.assignment_revision,
            execution_run_id=eligibility.execution_run_id,
            execution_task_id=eligibility.execution_task_id,
            attempt=attempt,
            environment_plan_version=self._readiness.inspect(
                str(eligibility.mission_id)
            ).environment_plan_version,
        )

    def inspect_completion(self, mission_id: str) -> MissionCompletionReadiness:
        mission = self._missions.mission(mission_id)
        assignments = tuple(self._latest_assignments(mission_id).values())
        run_bindings = {
            assignment.task_id: self._binding_for_assignment(mission_id, assignment)
            for assignment in assignments
        }
        blockers: list[str] = []
        try:
            MissionAssignmentGraphValidator.validate(assignments)
        except MishkanError as error:
            blockers.append(error.envelope.message)
        if mission.state is not MissionState.EVALUATING:
            blockers.append("mission must be evaluating before completion")
        environment = {item.task_id: item for item in self._readiness.inspect(mission_id).tasks}
        reported_runs = {
            item.run_id for item in self._missions.run_reports(mission_id, limit=1_000)
        }
        bound_runs = {
            item.execution_run_id for item in assignments if item.execution_run_id is not None
        }
        for run_id in sorted(bound_runs):
            if self._runs.task_count(run_id) > 1 and run_id not in reported_runs:
                blockers.append(f"multi-task run {run_id} has no versioned mission report")
        task_statuses: list[MissionTaskAcceptanceStatus] = []
        for assignment in assignments:
            readiness = environment.get(assignment.task_id)
            environment_ready = readiness is not None and readiness.environment_ready
            run_state: str | None = None
            task_state: str | None = None
            run_binding = run_bindings[assignment.task_id]
            if assignment.execution_run_id is None or assignment.execution_task_id is None:
                blockers.append(f"task {assignment.task_id} has no durable run binding")
            else:
                if run_binding is None:
                    blockers.append(
                        f"task {assignment.task_id} has no exact mission run binding for its "
                        "assignment revision"
                    )
                elif run_binding.acceptance is not MissionRunAcceptance.ACCEPTED:
                    blockers.append(
                        f"task {assignment.task_id} mission run binding is not accepted"
                    )
                try:
                    states = self._runs.task_states(assignment.execution_run_id)
                    task_state = states.get(assignment.execution_task_id)
                    run_state = self._runs.run_state(assignment.execution_run_id)
                except MishkanError as error:
                    if error.envelope.code is not ErrorCode.RUN_INTERRUPTED:
                        raise
                    blockers.append(f"task {assignment.task_id} bound run does not exist")
                if task_state != TaskState.ACCEPTED.value:
                    blockers.append(f"task {assignment.task_id} is not durably accepted")
                if run_state != RunState.COMPLETED.value:
                    blockers.append(f"task {assignment.task_id} bound run is not complete")
            if not environment_ready:
                blockers.append(f"task {assignment.task_id} environment is not currently ready")
            if assignment.crew_version != mission.current_crew_version:
                blockers.append(
                    f"task {assignment.task_id} assignment does not reference the current crew"
                )
            task_statuses.append(
                MissionTaskAcceptanceStatus(
                    task_id=assignment.task_id,
                    assignment_revision=assignment.assignment_revision,
                    assignment_kind=assignment.assignment_kind,
                    execution_run_id=assignment.execution_run_id,
                    execution_task_id=assignment.execution_task_id,
                    run_state=run_state,
                    run_task_state=task_state,
                    mission_run_acceptance=(
                        run_binding.acceptance if run_binding is not None else None
                    ),
                    environment_ready=environment_ready,
                    accepted=(
                        task_state == TaskState.ACCEPTED.value
                        and run_state == RunState.COMPLETED.value
                        and run_binding is not None
                        and run_binding.acceptance is MissionRunAcceptance.ACCEPTED
                        and environment_ready
                        and assignment.crew_version == mission.current_crew_version
                    ),
                )
            )
        unique_blockers = tuple(dict.fromkeys(blockers))
        return MissionCompletionReadiness(
            mission_id=mission.mission_id,
            mission_revision=mission.revision,
            mission_state=mission.state,
            ready=not unique_blockers and bool(task_statuses),
            tasks=tuple(task_statuses),
            blockers=unique_blockers,
        )

    def require_completion_ready(self, mission_id: str) -> MissionCompletionReadiness:
        readiness = self.inspect_completion(mission_id)
        if not readiness.ready:
            raise MishkanError(
                ErrorCode.MISSION,
                "mission is not ready for durable completion",
                details={"blockers": readiness.blockers},
            )
        return readiness

    def _latest_assignments(self, mission_id: str) -> dict[str, MissionTaskAssignment]:
        latest: dict[str, MissionTaskAssignment] = {}
        for assignment in self._missions.assignments(mission_id):
            current = latest.get(assignment.task_id)
            if current is None or assignment.assignment_revision > current.assignment_revision:
                latest[assignment.task_id] = assignment
        return latest

    def _binding_for_assignment(
        self,
        mission_id: str,
        assignment: MissionTaskAssignment,
    ) -> MissionRunBinding | None:
        latest: dict[str, MissionRunBinding] = {}
        for binding in self._missions.run_bindings(mission_id, limit=1_000):
            current = latest.get(binding.binding_key)
            if current is None or binding.binding_revision > current.binding_revision:
                latest[binding.binding_key] = binding
        matches = tuple(
            binding
            for binding in latest.values()
            if binding.schema_version == "1.1"
            and binding.mission_task_id == assignment.task_id
            and binding.assignment_id == assignment.assignment_id
            and binding.assignment_revision == assignment.assignment_revision
            and binding.run_id == assignment.execution_run_id
            and binding.execution_task_id == assignment.execution_task_id
        )
        return matches[0] if len(matches) == 1 else None

    def _latest_scoped_intervention(
        self,
        mission_id: str,
        assignment: MissionTaskAssignment,
    ) -> MissionIntervention | None:
        matching = [
            item
            for item in self._conversations.interventions(mission_id, limit=1_000)
            if item.target_kind.value == "task"
            and (
                item.target_id == assignment.task_id or self._scope_matches(item.scope, assignment)
            )
            and item.kind.value in {"suspend", "resume", "stop"}
        ]
        return matching[-1] if matching else None

    @staticmethod
    def _scope_matches(scope: tuple[str, ...], assignment: MissionTaskAssignment) -> bool:
        exact = {
            assignment.task_id,
            f"task:{assignment.task_id}",
            str(assignment.mission_id),
            f"mission:{assignment.mission_id}",
        }
        exact.update(assignment.environment_context_ids)
        exact.update(f"environment-context:{item}" for item in assignment.environment_context_ids)
        return bool(exact.intersection(scope))
