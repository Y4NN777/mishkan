"""Mission-scoped task claim gate over durable run execution."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import require_aware, utc_now
from mishkan.missions.models import MissionRecord, MissionState, MissionTaskAssignment
from mishkan.missions.readiness import (
    MissionEnvironmentReadinessService,
    MissionTaskEnvironmentReadiness,
)
from mishkan.runtime import TaskState

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


class MissionExecutionRepository(Protocol):
    def mission(self, mission_id: str) -> MissionRecord: ...

    def assignments(
        self, mission_id: str, *, limit: int = 1_000
    ) -> tuple[MissionTaskAssignment, ...]: ...


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

    def claim_task(self, run_id: str, task_id: str) -> int: ...


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
        assignment = self._assignment(mission_id, task_id)
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
            run_state = self._runs.task_states(assignment.execution_run_id).get(
                assignment.execution_task_id
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

    def _assignment(self, mission_id: str, task_id: str) -> MissionTaskAssignment:
        matches = [
            item for item in self._missions.assignments(mission_id) if item.task_id == task_id
        ]
        if not matches:
            raise MishkanError(ErrorCode.MISSION, "mission task assignment does not exist")
        return max(matches, key=lambda item: item.assignment_revision)

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
