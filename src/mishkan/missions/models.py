"""Public mission, Mission Brief, and contextual crew contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.domain.identity import new_id
from mishkan.domain.time import require_aware, utc_now


class MissionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MissionOriginKind(StrEnum):
    CEO = "ceo"
    PM = "pm"
    CTO = "cto"
    INCIDENT = "incident"
    PROJECT_EVIDENCE = "project_evidence"
    INDEPENDENT_FINDING = "independent_finding"
    DEPENDENCY = "dependency"
    MAINTENANCE = "maintenance"
    ORGANIZATIONAL_PROPOSAL = "organizational_proposal"


class MissionState(StrEnum):
    PROPOSED = "proposed"
    CLARIFYING = "clarifying"
    PLANNED = "planned"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    EVALUATING = "evaluating"
    REMEDIATING = "remediating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MissionBriefStatus(StrEnum):
    CLARIFYING = "clarifying"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class CrewAssignmentKind(StrEnum):
    PRODUCTION = "production"
    CONTRIBUTOR = "contributor"
    EVALUATION = "evaluation"
    REPORTING = "reporting"
    AUDIT = "audit"


class MissionOrigin(MissionModel):
    schema_version: Literal["1.0"] = "1.0"
    origin_id: UUID = Field(default_factory=new_id)
    kind: MissionOriginKind
    actor_id: str = Field(min_length=1, max_length=256)
    objective: str = Field(min_length=3, max_length=8_192)
    source_references: tuple[str, ...] = ()
    template_id: str | None = Field(default=None, min_length=1, max_length=256)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class MissionEnvironmentIntent(MissionModel):
    known_locations: tuple[str, ...] = ()
    target_platforms: tuple[str, ...] = ()
    target_architectures: tuple[str, ...] = ()
    existing_definition_references: tuple[str, ...] = ()
    isolation_requirements: tuple[str, ...] = ()
    network_requirements: tuple[str, ...] = ()
    credential_references: tuple[str, ...] = ()
    resource_constraints: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    known_unknowns: tuple[str, ...] = ()
    environment_dependent: bool

    @model_validator(mode="after")
    def dependent_work_declares_evidence_or_unknowns(self) -> MissionEnvironmentIntent:
        if self.environment_dependent and not (self.required_evidence or self.known_unknowns):
            raise ValueError(
                "environment-dependent work requires evidence requirements or explicit unknowns"
            )
        return self


class ExecutiveConfirmation(MissionModel):
    confirmation_id: UUID = Field(default_factory=new_id)
    identity_id: Literal["PM", "CTO"]
    disposition: Literal["confirmed", "rejected"]
    rationale: str = Field(min_length=3, max_length=4_096)
    evidence_references: tuple[str, ...] = Field(min_length=1)
    coverage: tuple[str, ...] = Field(min_length=1)
    confirmed_at: datetime = Field(default_factory=utc_now)

    @field_validator("confirmed_at")
    @classmethod
    def confirmed_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class MissionBrief(MissionModel):
    schema_version: Literal["1.0"] = "1.0"
    brief_id: UUID = Field(default_factory=new_id)
    mission_id: UUID
    version: int = Field(ge=1)
    organization_id: str = Field(min_length=1, max_length=128)
    organization_version: str = Field(min_length=1, max_length=64)
    status: MissionBriefStatus
    objective: str = Field(min_length=3, max_length=8_192)
    problem: str = Field(min_length=3, max_length=8_192)
    desired_outcome: str = Field(min_length=3, max_length=8_192)
    scope: tuple[str, ...] = Field(min_length=1)
    exclusions: tuple[str, ...]
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    constraints: tuple[str, ...]
    risks: tuple[str, ...]
    authority_scope: tuple[str, ...] = Field(min_length=1)
    proposed_crew: tuple[str, ...] = Field(min_length=2)
    evidence_requirements: tuple[str, ...] = Field(min_length=1)
    escalation_conditions: tuple[str, ...] = Field(min_length=1)
    environment_intent: MissionEnvironmentIntent
    pm_confirmation: ExecutiveConfirmation | None = None
    cto_confirmation: ExecutiveConfirmation | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def status_matches_executive_confirmations(self) -> MissionBrief:
        if self.pm_confirmation is not None and self.pm_confirmation.identity_id != "PM":
            raise ValueError("PM confirmation must be attributable to PM")
        if self.cto_confirmation is not None and self.cto_confirmation.identity_id != "CTO":
            raise ValueError("CTO confirmation must be attributable to CTO")
        confirmations = tuple(
            item for item in (self.pm_confirmation, self.cto_confirmation) if item is not None
        )
        if self.status is MissionBriefStatus.CONFIRMED and (
            len(confirmations) != 2
            or any(item.disposition != "confirmed" for item in confirmations)
        ):
            raise ValueError("confirmed Mission Brief requires PM and CTO confirmation")
        if self.status is MissionBriefStatus.REJECTED and not any(
            item.disposition == "rejected" for item in confirmations
        ):
            raise ValueError("rejected Mission Brief requires an attributable rejection")
        if len(self.proposed_crew) != len(set(self.proposed_crew)):
            raise ValueError("proposed crew identities must be unique")
        return self

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"brief_id"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class CrewSelectionEvidence(MissionModel):
    project_references: tuple[str, ...] = Field(min_length=1)
    competence_references: tuple[str, ...] = Field(min_length=1)
    availability_references: tuple[str, ...] = Field(min_length=1)
    conflict_assessment: str = Field(min_length=3, max_length=2_048)
    risk_coverage: tuple[str, ...] = Field(min_length=1)
    independence_references: tuple[str, ...] = Field(min_length=1)


class MissionCrewMember(MissionModel):
    identity_id: str = Field(min_length=2, max_length=128)
    assignment_kind: CrewAssignmentKind
    responsibility: str = Field(min_length=3, max_length=2_048)
    selection_evidence: CrewSelectionEvidence


class MissionCrewRevision(MissionModel):
    schema_version: Literal["1.0"] = "1.0"
    crew_id: UUID = Field(default_factory=new_id)
    mission_id: UUID
    version: int = Field(ge=1)
    organization_id: str = Field(min_length=1, max_length=128)
    organization_version: str = Field(min_length=1, max_length=64)
    brief_version: int = Field(ge=1)
    mission_lead_id: str = Field(min_length=2, max_length=128)
    members: tuple[MissionCrewMember, ...] = Field(min_length=2)
    pm_composition_confirmation_id: UUID
    cto_coverage_confirmation_id: UUID
    revision_reason: str = Field(min_length=3, max_length=4_096)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def crew_has_one_identity_per_separated_responsibility(self) -> MissionCrewRevision:
        identity_ids = [member.identity_id for member in self.members]
        if len(identity_ids) != len(set(identity_ids)):
            raise ValueError("Mission Crew identities must be unique")
        if self.mission_lead_id not in identity_ids:
            raise ValueError("Mission Lead must be assigned from the Mission Crew")
        assignment_by_identity = {
            member.identity_id: member.assignment_kind for member in self.members
        }
        if assignment_by_identity[self.mission_lead_id] is CrewAssignmentKind.REPORTING:
            raise ValueError("Mission Lead cannot report the mission it coordinates")
        return self

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"crew_id"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class MissionRecord(MissionModel):
    schema_version: Literal["1.0"] = "1.0"
    mission_id: UUID = Field(default_factory=new_id)
    revision: int = Field(default=0, ge=0)
    state: MissionState = MissionState.PROPOSED
    origin: MissionOrigin
    organization_id: str = Field(min_length=1, max_length=128)
    organization_version: str = Field(min_length=1, max_length=64)
    current_brief_version: int | None = Field(default=None, ge=1)
    current_crew_version: int | None = Field(default=None, ge=1)
    current_environment_plan_version: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def origin_objective_is_the_initial_mission_objective(self) -> MissionRecord:
        if self.state not in {MissionState.PROPOSED, MissionState.CLARIFYING} and (
            self.current_brief_version is None
        ):
            raise ValueError("advanced mission states require a durable Mission Brief")
        return self


class MissionResourceLimit(MissionModel):
    name: str = Field(min_length=1, max_length=128)
    value: int = Field(ge=0)
    unit: str = Field(min_length=1, max_length=64)


class MissionTaskAssignment(MissionModel):
    schema_version: Literal["1.0"] = "1.0"
    assignment_id: UUID = Field(default_factory=new_id)
    mission_id: UUID
    crew_version: int = Field(ge=1)
    task_id: str = Field(min_length=1, max_length=256)
    assignment_revision: int = Field(default=1, ge=1)
    accountable_owner: str = Field(min_length=2, max_length=128)
    contributors: tuple[str, ...] = ()
    expected_result: str = Field(min_length=3, max_length=8_192)
    completion_criteria: tuple[str, ...] = Field(min_length=1)
    dependencies: tuple[str, ...] = ()
    environment_context_ids: tuple[str, ...] = ()
    authority_scope: tuple[str, ...] = Field(min_length=1)
    exact_tools: tuple[str, ...]
    path_scopes: tuple[str, ...]
    limits: tuple[MissionResourceLimit, ...] = Field(min_length=1)
    required_evidence: tuple[str, ...] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def ownership_is_unambiguous(self) -> MissionTaskAssignment:
        if self.accountable_owner in self.contributors:
            raise ValueError("accountable owner cannot also be listed as a contributor")
        if len(self.contributors) != len(set(self.contributors)):
            raise ValueError("task contributors must be unique")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("task dependencies must be unique")
        if len(self.environment_context_ids) != len(set(self.environment_context_ids)):
            raise ValueError("task environment context dependencies must be unique")
        return self


class MissionTransition(MissionModel):
    schema_version: Literal["1.0"] = "1.0"
    transition_id: UUID = Field(default_factory=new_id)
    mission_id: UUID
    from_state: MissionState
    to_state: MissionState
    actor_or_cause: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=3, max_length=8_192)
    decision_id: UUID | None = None
    affected_scope: tuple[str, ...] = Field(min_length=1)
    evidence_references: tuple[str, ...] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def transition_changes_state(self) -> MissionTransition:
        if self.from_state is self.to_state:
            raise ValueError("mission transition must change state")
        return self
