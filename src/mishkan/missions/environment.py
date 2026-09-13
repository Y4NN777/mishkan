"""Agent-authored mission environment planning contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.identity import new_id
from mishkan.domain.time import require_aware, utc_now
from mishkan.environment import (
    AvailabilityState,
    EnvironmentBindingRequest,
    EnvironmentObservation,
    EnvironmentOutcome,
    EnvironmentProfile,
)
from mishkan.missions.models import (
    CrewAssignmentKind,
    MissionBrief,
    MissionCrewRevision,
    MissionRecord,
    MissionTaskAssignment,
)


class MissionEnvironmentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MissionEnvironmentAlternative(MissionEnvironmentModel):
    """One compatibility candidate exposed to planning without selecting it."""

    alternative_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    requested_outcome: EnvironmentOutcome
    required_semantics: tuple[str, ...] = ()
    allowed_descriptor_formats: tuple[str, ...] = ()
    required_engine_ids: tuple[str, ...] = ()
    eligible_engine_ids: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    evidence_references: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def outcome_has_compatible_shape(self) -> MissionEnvironmentAlternative:
        if self.requested_outcome is EnvironmentOutcome.UNRESOLVED and (
            self.required_engine_ids or self.eligible_engine_ids or self.allowed_descriptor_formats
        ):
            raise ValueError("unresolved alternative cannot advertise a compatible binding")
        if (
            self.requested_outcome
            in {
                EnvironmentOutcome.GENERATE,
                EnvironmentOutcome.PROPOSE_PROJECT_CHANGE,
            }
            and not self.allowed_descriptor_formats
        ):
            raise ValueError("generated environment alternative requires descriptor constraints")
        if set(self.required_engine_ids) - set(self.eligible_engine_ids):
            raise ValueError("required engines must be present in the eligible candidate set")
        return self


class MissionEnvironmentContextRequest(MissionEnvironmentModel):
    context_id: str = Field(min_length=1, max_length=256)
    observation_id: UUID
    observation_revision: int = Field(ge=1)
    observation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_platform: str = Field(min_length=1, max_length=128)
    target_architecture: str = Field(min_length=1, max_length=128)
    execution_location: str = Field(min_length=1, max_length=512)
    affected_task_ids: tuple[str, ...] = Field(min_length=1)
    alternatives: tuple[MissionEnvironmentAlternative, ...] = Field(min_length=1, max_length=64)
    evidence_references: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def identities_are_unique(self) -> MissionEnvironmentContextRequest:
        if len(self.affected_task_ids) != len(set(self.affected_task_ids)):
            raise ValueError("environment context task identities must be unique")
        ids = [item.alternative_id for item in self.alternatives]
        if len(ids) != len(set(ids)):
            raise ValueError("environment alternative identities must be unique per context")
        return self


class MissionEnvironmentPlanningRequest(MissionEnvironmentModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: UUID = Field(default_factory=new_id)
    mission_id: UUID
    mission_revision: int = Field(ge=1)
    brief_version: int = Field(ge=1)
    crew_version: int = Field(ge=1)
    planning_task_id: str = Field(min_length=1, max_length=256)
    owner_identity: str = Field(min_length=2, max_length=128)
    contexts: tuple[MissionEnvironmentContextRequest, ...] = Field(min_length=1, max_length=64)
    evidence_references: tuple[str, ...] = Field(min_length=1)
    requested_at: datetime = Field(default_factory=utc_now)

    @field_validator("requested_at")
    @classmethod
    def requested_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def context_identities_are_unique(self) -> MissionEnvironmentPlanningRequest:
        ids = [item.context_id for item in self.contexts]
        if len(ids) != len(set(ids)):
            raise ValueError("mission environment context identities must be unique")
        return self


class MissionEnvironmentDecision(MissionEnvironmentModel):
    context_id: str = Field(min_length=1, max_length=256)
    selected_alternative_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    requested_outcome: EnvironmentOutcome
    rationale: str = Field(min_length=3, max_length=8_192)
    constraints: tuple[str, ...]
    declared_effects: tuple[str, ...]
    verification_checks: tuple[str, ...] = Field(min_length=1)
    cleanup_criteria: tuple[str, ...] = Field(min_length=1)
    alternatives_considered: tuple[str, ...] = Field(min_length=1)
    unknowns: tuple[str, ...]
    evidence_references: tuple[str, ...] = Field(min_length=1)
    required_semantics: tuple[str, ...] = ()
    allowed_descriptor_formats: tuple[str, ...] = ()
    required_engine_ids: tuple[str, ...] = ()
    eligible_engine_ids: tuple[str, ...] = ()


class CrewAIPlanningLineage(MissionEnvironmentModel):
    runtime: Literal["crewai-1.x"] = "crewai-1.x"
    coordination_id: UUID = Field(default_factory=new_id)
    task_id: UUID = Field(default_factory=new_id)
    model_route: str = Field(min_length=1, max_length=256)
    output_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    produced_at: datetime = Field(default_factory=utc_now)

    @field_validator("produced_at")
    @classmethod
    def produced_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class MissionEnvironmentPlan(MissionEnvironmentModel):
    schema_version: Literal["1.0"] = "1.0"
    plan_id: UUID = Field(default_factory=new_id)
    mission_id: UUID
    version: int = Field(ge=1)
    mission_revision: int = Field(ge=1)
    brief_version: int = Field(ge=1)
    crew_version: int = Field(ge=1)
    source_request_id: UUID
    planning_task_id: str = Field(min_length=1, max_length=256)
    owner_identity: str = Field(min_length=2, max_length=128)
    evidence_references: tuple[str, ...] = Field(min_length=1)
    contexts: tuple[MissionEnvironmentContextRequest, ...] = Field(min_length=1, max_length=64)
    decisions: tuple[MissionEnvironmentDecision, ...] = Field(min_length=1, max_length=64)
    lineage: CrewAIPlanningLineage
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def decisions_cover_exact_contexts(self) -> MissionEnvironmentPlan:
        context_ids = [item.context_id for item in self.contexts]
        decision_ids = [item.context_id for item in self.decisions]
        if len(decision_ids) != len(set(decision_ids)) or set(decision_ids) != set(context_ids):
            raise ValueError("environment decisions must cover every context exactly once")
        return self

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"plan_id"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def binding_request(
        self,
        context_id: str,
        *,
        policy_fingerprint: str,
    ) -> EnvironmentBindingRequest:
        contexts = {item.context_id: item for item in self.contexts}
        decisions = {item.context_id: item for item in self.decisions}
        try:
            context = contexts[context_id]
            decision = decisions[context_id]
        except KeyError as exc:
            raise ValueError(
                "mission environment plan does not contain the requested context"
            ) from exc
        return EnvironmentBindingRequest(
            request_id=uuid5(self.plan_id, f"context:{context_id}"),
            mission_id=str(self.mission_id),
            plan_fingerprint=self.fingerprint,
            owner_identity=self.owner_identity,
            context_id=context.context_id,
            observation_id=context.observation_id,
            observation_revision=context.observation_revision,
            observation_fingerprint=context.observation_fingerprint,
            requested_outcome=decision.requested_outcome,
            target_platform=context.target_platform,
            target_architecture=context.target_architecture,
            execution_location=context.execution_location,
            required_semantics=decision.required_semantics,
            allowed_descriptor_formats=decision.allowed_descriptor_formats,
            required_engine_ids=decision.required_engine_ids,
            authorized_engine_ids=decision.eligible_engine_ids,
            affected_task_ids=context.affected_task_ids,
            verification_checks=decision.verification_checks,
            policy_fingerprint=policy_fingerprint,
            rationale=decision.rationale,
        )


class MissionEnvironmentPlanAcceptance(MissionEnvironmentModel):
    schema_version: Literal["1.0"] = "1.0"
    plan: MissionEnvironmentPlan
    accepted_by: str = Field(min_length=1, max_length=256)
    policy_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    accepted_at: datetime = Field(default_factory=utc_now)

    @field_validator("accepted_at")
    @classmethod
    def accepted_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class MissionEnvironmentPlanValidator:
    """Validate planning lineage and choices without selecting an outcome."""

    @classmethod
    def validate_request(
        cls,
        request: MissionEnvironmentPlanningRequest,
        *,
        mission: MissionRecord,
        brief: MissionBrief,
        crew: MissionCrewRevision,
        assignments: tuple[MissionTaskAssignment, ...],
        observations: tuple[EnvironmentObservation, ...],
        profile: EnvironmentProfile | None = None,
    ) -> None:
        if request.mission_id != mission.mission_id or request.mission_revision != mission.revision:
            raise MishkanError(
                ErrorCode.REVISION_MISMATCH,
                "mission changed before environment planning",
            )
        if request.brief_version != brief.version or request.crew_version != crew.version:
            raise MishkanError(
                ErrorCode.REVISION_MISMATCH,
                "environment planning request does not reference the current Brief and crew",
            )
        if not brief.environment_intent.environment_dependent:
            raise MishkanError(
                ErrorCode.PLAN,
                "mission does not declare environment-dependent work",
            )
        members = {item.identity_id: item for item in crew.members}
        owner = members.get(request.owner_identity)
        if owner is None:
            raise MishkanError(
                ErrorCode.ROLE_CONFLICT,
                "environment plan owner is not a member of the current Mission Crew",
            )
        if owner.assignment_kind in {
            CrewAssignmentKind.EVALUATION,
            CrewAssignmentKind.REPORTING,
            CrewAssignmentKind.AUDIT,
        }:
            raise MishkanError(
                ErrorCode.ROLE_CONFLICT,
                "independent assurance or reporting identity cannot own "
                "environment production planning",
            )
        latest = cls._latest_assignments(assignments)
        planning_assignment = latest.get(request.planning_task_id)
        if planning_assignment is None or (
            planning_assignment.accountable_owner != request.owner_identity
        ):
            raise MishkanError(
                ErrorCode.ROLE_CONFLICT,
                "environment planning task is not assigned to the declared accountable owner",
            )
        observed = {item.observation_id: item for item in observations}
        declared_by_context: dict[str, set[str]] = {}
        for assignment in latest.values():
            for context_id in assignment.environment_context_ids:
                declared_by_context.setdefault(context_id, set()).add(assignment.task_id)
        requested_context_ids = {item.context_id for item in request.contexts}
        if undeclared_contexts := set(declared_by_context) - requested_context_ids:
            raise MishkanError(
                ErrorCode.PLAN,
                "environment planning omits declared task contexts",
                details={"context_ids": sorted(undeclared_contexts)},
            )
        for context in request.contexts:
            unknown_tasks = set(context.affected_task_ids) - set(latest)
            if unknown_tasks:
                raise MishkanError(
                    ErrorCode.PLAN,
                    "environment context references unassigned mission tasks",
                    details={"unknown_task_ids": sorted(unknown_tasks)},
                )
            declared_tasks = declared_by_context.get(context.context_id, set())
            if set(context.affected_task_ids) != declared_tasks:
                raise MishkanError(
                    ErrorCode.PLAN,
                    "environment context task scope differs from assignment dependencies",
                    details={
                        "context_id": context.context_id,
                        "declared_task_ids": sorted(declared_tasks),
                        "received_task_ids": sorted(context.affected_task_ids),
                    },
                )
            observation = observed.get(context.observation_id)
            if observation is None:
                raise MishkanError(
                    ErrorCode.REQUIRED_DEPENDENCY,
                    "environment planning observation does not exist",
                )
            cls._validate_context(context, observation, profile)

    @classmethod
    def validate_plan(
        cls,
        plan: MissionEnvironmentPlan,
        *,
        mission: MissionRecord,
        brief: MissionBrief,
        crew: MissionCrewRevision,
        assignments: tuple[MissionTaskAssignment, ...],
        observations: tuple[EnvironmentObservation, ...],
        profile: EnvironmentProfile | None = None,
    ) -> None:
        request = MissionEnvironmentPlanningRequest(
            request_id=plan.source_request_id,
            mission_id=plan.mission_id,
            mission_revision=plan.mission_revision,
            brief_version=plan.brief_version,
            crew_version=plan.crew_version,
            planning_task_id=plan.planning_task_id,
            owner_identity=plan.owner_identity,
            contexts=plan.contexts,
            evidence_references=plan.evidence_references,
        )
        cls.validate_request(
            request,
            mission=mission,
            brief=brief,
            crew=crew,
            assignments=assignments,
            observations=observations,
            profile=profile,
        )
        for context, decision in zip(
            sorted(plan.contexts, key=lambda item: item.context_id),
            sorted(plan.decisions, key=lambda item: item.context_id),
            strict=True,
        ):
            alternatives = {item.alternative_id: item for item in context.alternatives}
            selected = alternatives.get(decision.selected_alternative_id)
            if selected is None or decision.requested_outcome is not selected.requested_outcome:
                raise MishkanError(
                    ErrorCode.PLAN,
                    "CrewAI environment decision does not select an exposed alternative",
                )
            if set(decision.alternatives_considered) != set(alternatives):
                raise MishkanError(
                    ErrorCode.PLAN,
                    "CrewAI environment decision did not compare every eligible alternative",
                )
            expected = (
                selected.required_semantics,
                selected.allowed_descriptor_formats,
                selected.required_engine_ids,
                selected.eligible_engine_ids,
            )
            received = (
                decision.required_semantics,
                decision.allowed_descriptor_formats,
                decision.required_engine_ids,
                decision.eligible_engine_ids,
            )
            if received != expected:
                raise MishkanError(
                    ErrorCode.PLAN,
                    "environment plan altered resolver constraints after choosing an alternative",
                )
            attributable = {
                *plan.evidence_references,
                *context.evidence_references,
                *selected.evidence_references,
            }
            if set(decision.evidence_references) - attributable:
                raise MishkanError(
                    ErrorCode.PLAN,
                    "environment decision references evidence absent from its planning input",
                )

    @staticmethod
    def _latest_assignments(
        assignments: tuple[MissionTaskAssignment, ...],
    ) -> dict[str, MissionTaskAssignment]:
        latest: dict[str, MissionTaskAssignment] = {}
        for assignment in assignments:
            current = latest.get(assignment.task_id)
            if current is None or assignment.assignment_revision > current.assignment_revision:
                latest[assignment.task_id] = assignment
        return latest

    @staticmethod
    def _validate_context(
        context: MissionEnvironmentContextRequest,
        observation: EnvironmentObservation,
        profile: EnvironmentProfile | None,
    ) -> None:
        if (
            context.context_id != observation.context_id
            or context.observation_revision != observation.revision
            or context.observation_fingerprint != observation.fingerprint
            or context.target_platform != observation.platform
            or context.target_architecture != observation.architecture
            or context.execution_location != observation.execution_location
        ):
            raise MishkanError(
                ErrorCode.REVISION_MISMATCH,
                "environment planning context differs from its observation",
            )
        engines = {item.engine_id: item for item in observation.engines}
        adapters = (
            {item.adapter_id: item for item in profile.adapters} if profile is not None else {}
        )
        observed_formats = {item.format for item in observation.descriptors}
        for alternative in context.alternatives:
            if alternative.requested_outcome is EnvironmentOutcome.UNRESOLVED:
                continue
            if alternative.requested_outcome is EnvironmentOutcome.REUSE_EXISTING and (
                not observed_formats
                or (
                    alternative.allowed_descriptor_formats
                    and not observed_formats.intersection(alternative.allowed_descriptor_formats)
                )
            ):
                raise MishkanError(
                    ErrorCode.PLAN,
                    "reuse alternative has no matching observed descriptor",
                )
            if not alternative.eligible_engine_ids and alternative.requested_outcome is not (
                EnvironmentOutcome.REUSE_EXISTING
            ):
                raise MishkanError(
                    ErrorCode.PLAN,
                    "executable environment alternative has no eligible engine",
                )
            for engine_id in alternative.eligible_engine_ids:
                engine = engines.get(engine_id)
                if engine is None or engine.fact("eligible") is not AvailabilityState.TRUE:
                    raise MishkanError(
                        ErrorCode.PLAN,
                        "environment alternative claims an engine that is not observed eligible",
                    )
                if not set(alternative.required_semantics).issubset(engine.semantics):
                    raise MishkanError(
                        ErrorCode.PLAN,
                        "environment alternative requires unsupported engine semantics",
                    )
                if profile is not None:
                    adapter = adapters.get(engine.adapter_id or "")
                    if adapter is None or adapter.engine_id != engine.engine_id:
                        raise MishkanError(
                            ErrorCode.PLAN,
                            "environment alternative has no compatible configured adapter",
                        )
                    if set(alternative.allowed_descriptor_formats) - set(
                        adapter.descriptor_formats
                    ):
                        raise MishkanError(
                            ErrorCode.PLAN,
                            "environment alternative requests a descriptor unsupported "
                            "by its adapter",
                        )
