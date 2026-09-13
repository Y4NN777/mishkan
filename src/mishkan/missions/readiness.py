"""Truthful mission-task readiness derived from accepted environment evidence."""

from __future__ import annotations

from collections.abc import Set
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from mishkan.environment import (
    EnvironmentBinding,
    EnvironmentBindingState,
    EnvironmentSettlement,
    EnvironmentVerification,
)
from mishkan.missions.environment import MissionEnvironmentPlanAcceptance
from mishkan.missions.models import MissionRecord, MissionTaskAssignment


class MissionReadinessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EnvironmentReadinessState(StrEnum):
    NOT_REQUIRED = "not_required"
    AWAITING_PLAN = "awaiting_plan"
    UNRESOLVED = "unresolved"
    SERVICE_UNAVAILABLE = "service_unavailable"
    AWAITING_BINDING = "awaiting_binding"
    INCOMPATIBLE = "incompatible"
    STALE = "stale"
    AWAITING_VERIFICATION = "awaiting_verification"
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_UNCERTAIN = "verification_uncertain"
    ELIGIBLE = "eligible"


class EnvironmentContextReadiness(MissionReadinessModel):
    context_id: str = Field(min_length=1, max_length=256)
    state: EnvironmentReadinessState
    plan_id: UUID | None = None
    plan_version: int | None = Field(default=None, ge=1)
    binding_id: UUID | None = None
    binding_revision: int | None = Field(default=None, ge=1)
    verification_id: UUID | None = None
    reason: str = Field(min_length=1, max_length=4_096)


class MissionTaskEnvironmentReadiness(MissionReadinessModel):
    task_id: str = Field(min_length=1, max_length=256)
    assignment_revision: int = Field(ge=1)
    state: EnvironmentReadinessState
    environment_ready: bool
    contexts: tuple[EnvironmentContextReadiness, ...]
    blockers: tuple[str, ...]


class MissionEnvironmentReadiness(MissionReadinessModel):
    schema_version: str = "1.0"
    mission_id: UUID
    mission_revision: int = Field(ge=0)
    environment_plan_version: int | None = Field(default=None, ge=1)
    tasks: tuple[MissionTaskEnvironmentReadiness, ...]
    ready_task_ids: tuple[str, ...]
    blocked_task_ids: tuple[str, ...]


class MissionReadinessRepository(Protocol):
    def mission(self, mission_id: str) -> MissionRecord: ...

    def assignments(
        self, mission_id: str, *, limit: int = 1_000
    ) -> tuple[MissionTaskAssignment, ...]: ...

    def environment_plan(
        self,
        mission_id: str,
        version: int | None = None,
    ) -> MissionEnvironmentPlanAcceptance: ...


class EnvironmentReadinessRepository(Protocol):
    def binding_for_request(self, request_id: str) -> EnvironmentBinding | None: ...

    def verifications_for_binding(
        self,
        binding_id: str,
        *,
        limit: int = 1_000,
    ) -> tuple[EnvironmentVerification, ...]: ...


class MissionEnvironmentReadinessService:
    """Derive eligibility without inventing a binding, verification, or success."""

    def __init__(
        self,
        missions: MissionReadinessRepository,
        environments: EnvironmentReadinessRepository | None,
    ) -> None:
        self._missions = missions
        self._environments = environments

    def inspect(self, mission_id: str) -> MissionEnvironmentReadiness:
        mission = self._missions.mission(mission_id)
        assignments = self._latest(self._missions.assignments(mission_id))
        acceptance = (
            self._missions.environment_plan(mission_id)
            if mission.current_environment_plan_version is not None
            else None
        )
        contexts = (
            {item.context_id: item for item in acceptance.plan.contexts}
            if acceptance is not None
            else {}
        )
        decisions = (
            {item.context_id: item for item in acceptance.plan.decisions}
            if acceptance is not None
            else {}
        )
        tasks = tuple(
            self._task_readiness(
                assignment,
                acceptance=acceptance,
                context_ids=set(contexts),
                decision_ids=set(decisions),
            )
            for assignment in sorted(assignments.values(), key=lambda item: item.task_id)
        )
        return MissionEnvironmentReadiness(
            mission_id=mission.mission_id,
            mission_revision=mission.revision,
            environment_plan_version=mission.current_environment_plan_version,
            tasks=tasks,
            ready_task_ids=tuple(item.task_id for item in tasks if item.environment_ready),
            blocked_task_ids=tuple(item.task_id for item in tasks if not item.environment_ready),
        )

    def _task_readiness(
        self,
        assignment: MissionTaskAssignment,
        *,
        acceptance: MissionEnvironmentPlanAcceptance | None,
        context_ids: Set[str],
        decision_ids: Set[str],
    ) -> MissionTaskEnvironmentReadiness:
        if not assignment.environment_context_ids:
            return MissionTaskEnvironmentReadiness(
                task_id=assignment.task_id,
                assignment_revision=assignment.assignment_revision,
                state=EnvironmentReadinessState.NOT_REQUIRED,
                environment_ready=True,
                contexts=(),
                blockers=(),
            )
        readiness = tuple(
            self._context_readiness(context_id, acceptance, context_ids, decision_ids)
            for context_id in assignment.environment_context_ids
        )
        ready = all(item.state is EnvironmentReadinessState.ELIGIBLE for item in readiness)
        state = (
            EnvironmentReadinessState.ELIGIBLE
            if ready
            else next(
                item.state
                for item in readiness
                if item.state is not EnvironmentReadinessState.ELIGIBLE
            )
        )
        return MissionTaskEnvironmentReadiness(
            task_id=assignment.task_id,
            assignment_revision=assignment.assignment_revision,
            state=state,
            environment_ready=ready,
            contexts=readiness,
            blockers=tuple(
                item.reason
                for item in readiness
                if item.state is not EnvironmentReadinessState.ELIGIBLE
            ),
        )

    def _context_readiness(
        self,
        context_id: str,
        acceptance: MissionEnvironmentPlanAcceptance | None,
        context_ids: Set[str],
        decision_ids: Set[str],
    ) -> EnvironmentContextReadiness:
        if acceptance is None or context_id not in context_ids or context_id not in decision_ids:
            return EnvironmentContextReadiness(
                context_id=context_id,
                state=EnvironmentReadinessState.AWAITING_PLAN,
                reason="no current accepted plan covers this declared task context",
            )
        plan = acceptance.plan
        decision = next(item for item in plan.decisions if item.context_id == context_id)
        if decision.requested_outcome.value == "unresolved":
            return EnvironmentContextReadiness(
                context_id=context_id,
                state=EnvironmentReadinessState.UNRESOLVED,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                reason="the accepted agent-authored decision remains unresolved",
            )
        if self._environments is None:
            return EnvironmentContextReadiness(
                context_id=context_id,
                state=EnvironmentReadinessState.SERVICE_UNAVAILABLE,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                reason="the environment evidence repository is unavailable",
            )
        request = plan.binding_request(
            context_id,
            policy_fingerprint=acceptance.policy_fingerprint,
        )
        binding = self._environments.binding_for_request(str(request.request_id))
        if binding is None:
            return EnvironmentContextReadiness(
                context_id=context_id,
                state=EnvironmentReadinessState.AWAITING_BINDING,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                reason="the accepted outcome has no resolved binding",
            )
        if binding.state is EnvironmentBindingState.STALE:
            return EnvironmentContextReadiness(
                context_id=context_id,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                binding_id=binding.binding_id,
                binding_revision=binding.revision,
                state=EnvironmentReadinessState.STALE,
                reason=binding.reason,
            )
        if binding.state is EnvironmentBindingState.INCOMPATIBLE:
            return EnvironmentContextReadiness(
                context_id=context_id,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                binding_id=binding.binding_id,
                binding_revision=binding.revision,
                state=EnvironmentReadinessState.INCOMPATIBLE,
                reason=binding.reason,
            )
        if binding.state is not EnvironmentBindingState.COMPATIBLE:
            return EnvironmentContextReadiness(
                context_id=context_id,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                binding_id=binding.binding_id,
                binding_revision=binding.revision,
                state=EnvironmentReadinessState.UNRESOLVED,
                reason=binding.reason,
            )
        verifications = self._environments.verifications_for_binding(str(binding.binding_id))
        current = next(
            (
                item
                for item in reversed(verifications)
                if item.context_fingerprint == request.observation_fingerprint
            ),
            None,
        )
        if current is None:
            return EnvironmentContextReadiness(
                context_id=context_id,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                binding_id=binding.binding_id,
                binding_revision=binding.revision,
                state=EnvironmentReadinessState.AWAITING_VERIFICATION,
                reason="compatible binding has no current location-bound verification",
            )
        if current.settlement is EnvironmentSettlement.VERIFIED:
            return EnvironmentContextReadiness(
                context_id=context_id,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                binding_id=binding.binding_id,
                binding_revision=binding.revision,
                verification_id=current.verification_id,
                state=EnvironmentReadinessState.ELIGIBLE,
                reason="accepted binding has current verified environment evidence",
            )
        state = (
            EnvironmentReadinessState.VERIFICATION_FAILED
            if current.settlement is EnvironmentSettlement.FAILED
            else EnvironmentReadinessState.VERIFICATION_UNCERTAIN
        )
        return EnvironmentContextReadiness(
            context_id=context_id,
            plan_id=plan.plan_id,
            plan_version=plan.version,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
            verification_id=current.verification_id,
            state=state,
            reason=f"environment verification settled as {current.settlement.value}",
        )

    @staticmethod
    def _latest(
        assignments: tuple[MissionTaskAssignment, ...],
    ) -> dict[str, MissionTaskAssignment]:
        latest: dict[str, MissionTaskAssignment] = {}
        for assignment in assignments:
            current = latest.get(assignment.task_id)
            if current is None or assignment.assignment_revision > current.assignment_revision:
                latest[assignment.task_id] = assignment
        return latest
