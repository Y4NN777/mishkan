"""Durable communication, escalation, decision, and intervention contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.domain.identity import new_id
from mishkan.domain.time import require_aware, utc_now
from mishkan.missions.models import MissionState


class ConversationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChannelClass(StrEnum):
    EXECUTIVE = "executive"
    MISSION = "mission"
    BRANCH = "branch"
    DIRECT = "direct"


class EscalationState(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    RESOLVED = "resolved"
    WITHDRAWN = "withdrawn"


class InterventionKind(StrEnum):
    COMMENT = "comment"
    ANSWER_ESCALATION = "answer_escalation"
    ACCEPT_PROPOSAL = "accept_proposal"
    REJECT_PROPOSAL = "reject_proposal"
    SUSPEND = "suspend"
    RESUME = "resume"
    REQUEST_REASSIGNMENT = "request_reassignment"
    CONFIRM_REASSIGNMENT = "confirm_reassignment"
    STOP = "stop"
    ACCEPT_RISK = "accept_risk"


class InterventionTargetKind(StrEnum):
    MISSION = "mission"
    TASK = "task"
    PROPOSAL = "proposal"
    ASSIGNMENT = "assignment"
    RISK = "risk"
    ESCALATION = "escalation"


class ConversationChannel(ConversationModel):
    schema_version: Literal["1.0"] = "1.0"
    conversation_id: UUID = Field(default_factory=new_id)
    channel_class: ChannelClass
    title: str = Field(min_length=3, max_length=512)
    participants: tuple[str, ...] = Field(min_length=2)
    mission_id: UUID | None = None
    branch_id: str | None = Field(default=None, min_length=1, max_length=128)
    direct_authorization_reference: str | None = Field(default=None, min_length=1, max_length=512)
    created_by: str = Field(min_length=1, max_length=256)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def channel_scope_matches_class(self) -> ConversationChannel:
        if len(self.participants) != len(set(self.participants)):
            raise ValueError("conversation participants must be unique")
        if self.channel_class is ChannelClass.EXECUTIVE:
            if not {"CEO", "PM", "CTO"}.issubset(self.participants):
                raise ValueError("Executive channel requires CEO, PM, and CTO")
            if self.mission_id is not None or self.branch_id is not None:
                raise ValueError("Executive channel is organization-scoped")
        elif self.channel_class is ChannelClass.MISSION:
            if self.mission_id is None:
                raise ValueError("Mission channel requires a mission identity")
        elif self.channel_class is ChannelClass.BRANCH:
            if self.branch_id is None:
                raise ValueError("Branch channel requires a branch identity")
        elif self.channel_class is ChannelClass.DIRECT:
            if len(self.participants) != 2 or self.direct_authorization_reference is None:
                raise ValueError("Direct channel requires two participants and explicit authority")
        return self


class ConversationMessage(ConversationModel):
    schema_version: Literal["1.0"] = "1.0"
    message_id: UUID = Field(default_factory=new_id)
    conversation_id: UUID
    author_identity: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=65_536)
    reply_to_message_id: UUID | None = None
    evidence_references: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class MissionDecision(ConversationModel):
    schema_version: Literal["1.0"] = "1.0"
    decision_id: UUID = Field(default_factory=new_id)
    mission_id: UUID
    conversation_id: UUID
    actor_id: str = Field(min_length=1, max_length=256)
    subject: str = Field(min_length=3, max_length=2_048)
    disposition: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=3, max_length=8_192)
    scope: tuple[str, ...] = Field(min_length=1)
    evidence_references: tuple[str, ...] = Field(min_length=1)
    authority_reference: str = Field(min_length=1, max_length=512)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class EscalationOption(ConversationModel):
    option_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=3, max_length=4_096)
    consequences: tuple[str, ...] = Field(min_length=1)
    risks: tuple[str, ...] = Field(min_length=1)


class ExecutiveRecommendation(ConversationModel):
    identity_id: Literal["PM", "CTO"]
    recommended_option_id: str = Field(min_length=1, max_length=128)
    rationale: str = Field(min_length=3, max_length=4_096)
    evidence_references: tuple[str, ...] = Field(min_length=1)


class MissionEscalation(ConversationModel):
    schema_version: Literal["1.0"] = "1.0"
    escalation_id: UUID = Field(default_factory=new_id)
    mission_id: UUID
    conversation_id: UUID
    state: EscalationState = EscalationState.OPEN
    raised_by: str = Field(min_length=1, max_length=256)
    blocked_scope: tuple[str, ...] = Field(min_length=1)
    decision_required: str = Field(min_length=3, max_length=8_192)
    reason: str = Field(min_length=3, max_length=8_192)
    options: tuple[EscalationOption, ...] = Field(min_length=2)
    recommendations: tuple[ExecutiveRecommendation, ...] = Field(min_length=1)
    uncertainty: tuple[str, ...] = Field(min_length=1)
    independent_work_continuing: tuple[str, ...]
    deadline: datetime | None = None
    evidence_references: tuple[str, ...] = Field(min_length=1)
    answer_intervention_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("deadline", "created_at", "updated_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_aware(value)

    @model_validator(mode="after")
    def escalation_is_actionable(self) -> MissionEscalation:
        option_ids = [option.option_id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("escalation option identities must be unique")
        if any(item.recommended_option_id not in option_ids for item in self.recommendations):
            raise ValueError("executive recommendation must reference an available option")
        if self.state is EscalationState.OPEN and self.answer_intervention_id is not None:
            raise ValueError("open escalation cannot already reference an answer")
        if self.state in {EscalationState.ANSWERED, EscalationState.RESOLVED} and (
            self.answer_intervention_id is None
        ):
            raise ValueError("answered escalation requires its intervention identity")
        return self


class MissionIntervention(ConversationModel):
    schema_version: Literal["1.0"] = "1.0"
    intervention_id: UUID = Field(default_factory=new_id)
    mission_id: UUID
    conversation_id: UUID
    actor_id: Literal["CEO"]
    kind: InterventionKind
    target_kind: InterventionTargetKind
    target_id: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=3, max_length=8_192)
    scope: tuple[str, ...] = Field(min_length=1)
    confirmation: str = Field(min_length=3, max_length=4_096)
    authority_reference: str = Field(min_length=1, max_length=512)
    evidence_references: tuple[str, ...] = Field(min_length=1)
    escalation_id: UUID | None = None
    effect: str = Field(min_length=3, max_length=4_096)
    resulting_mission_state: MissionState | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def kind_matches_target_and_state(self) -> MissionIntervention:
        allowed_targets = {
            InterventionKind.COMMENT: set(InterventionTargetKind),
            InterventionKind.ANSWER_ESCALATION: {InterventionTargetKind.ESCALATION},
            InterventionKind.ACCEPT_PROPOSAL: {InterventionTargetKind.PROPOSAL},
            InterventionKind.REJECT_PROPOSAL: {InterventionTargetKind.PROPOSAL},
            InterventionKind.SUSPEND: {
                InterventionTargetKind.MISSION,
                InterventionTargetKind.TASK,
            },
            InterventionKind.RESUME: {
                InterventionTargetKind.MISSION,
                InterventionTargetKind.TASK,
            },
            InterventionKind.REQUEST_REASSIGNMENT: {
                InterventionTargetKind.TASK,
                InterventionTargetKind.ASSIGNMENT,
            },
            InterventionKind.CONFIRM_REASSIGNMENT: {
                InterventionTargetKind.TASK,
                InterventionTargetKind.ASSIGNMENT,
            },
            InterventionKind.STOP: {
                InterventionTargetKind.MISSION,
                InterventionTargetKind.TASK,
            },
            InterventionKind.ACCEPT_RISK: {InterventionTargetKind.RISK},
        }[self.kind]
        if self.target_kind not in allowed_targets:
            raise ValueError("intervention kind does not support the declared target")
        if self.kind is InterventionKind.ANSWER_ESCALATION:
            if self.escalation_id is None or self.target_id != str(self.escalation_id):
                raise ValueError("escalation answer must target its exact escalation identity")
        elif self.escalation_id is not None:
            raise ValueError("only an escalation answer may carry an escalation identity")
        mission_result = {
            InterventionKind.SUSPEND: MissionState.PAUSED,
            InterventionKind.RESUME: MissionState.ACTIVE,
            InterventionKind.STOP: MissionState.CANCELLED,
        }.get(self.kind)
        if self.target_kind is InterventionTargetKind.MISSION:
            if mission_result is None and self.resulting_mission_state is not None:
                raise ValueError("non-lifecycle intervention cannot change mission state")
            if mission_result is not None and self.resulting_mission_state is not mission_result:
                raise ValueError("mission lifecycle intervention has an invalid resulting state")
        elif self.resulting_mission_state is not None:
            raise ValueError("scoped intervention cannot silently change the whole mission state")
        return self
