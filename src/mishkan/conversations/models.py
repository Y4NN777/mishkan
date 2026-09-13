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


class MessagePurpose(StrEnum):
    DISCUSSION = "discussion"
    COLLABORATION = "collaboration"
    CONSULTATION = "consultation"
    HANDOFF_CONTEXT = "handoff_context"
    REVIEW = "review"
    EVIDENCE_CHALLENGE = "evidence_challenge"


class EscalationState(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    RESOLVED = "resolved"
    WITHDRAWN = "withdrawn"


class DecisionEvidenceClass(StrEnum):
    VERIFIED = "verified"
    ASSUMPTION = "assumption"
    ENGINEER_PREFERENCE = "engineer_preference"
    INFERENCE = "inference"
    UNRESOLVED_UNKNOWN = "unresolved_unknown"


class DecisionStatus(StrEnum):
    STAGED = "staged"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class DecisionValidationStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class DecisionExplanationDepth(StrEnum):
    CONCISE = "concise"
    STANDARD = "standard"
    DEEP = "deep"


class DecisionExplanationPreference(ConversationModel):
    requested_by: str = Field(min_length=1, max_length=256)
    depth: DecisionExplanationDepth = DecisionExplanationDepth.STANDARD
    focus_areas: tuple[str, ...] = ()
    request_reference: str = Field(min_length=1, max_length=1_024)

    @field_validator("focus_areas")
    @classmethod
    def focus_areas_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("decision explanation focus areas must be unique")
        return value


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


class InterventionResultState(StrEnum):
    UNCHANGED = "unchanged"
    ANSWERED = "answered"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PAUSED = "paused"
    ACTIVE = "active"
    REASSIGNMENT_REQUESTED = "reassignment_requested"
    REASSIGNMENT_CONFIRMED = "reassignment_confirmed"
    CANCELLED = "cancelled"
    RISK_ACCEPTED = "risk_accepted"


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
    schema_version: Literal["1.0", "1.1"] = "1.0"
    message_id: UUID = Field(default_factory=new_id)
    conversation_id: UUID
    author_identity: str = Field(min_length=1, max_length=256)
    purpose: MessagePurpose = MessagePurpose.DISCUSSION
    body: str = Field(min_length=1, max_length=65_536)
    reply_to_message_id: UUID | None = None
    scope: tuple[str, ...] = ()
    evidence_references: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def collaborative_purpose_is_attributable(self) -> ConversationMessage:
        if self.schema_version == "1.0":
            if self.purpose is not MessagePurpose.DISCUSSION or self.scope:
                raise ValueError("message 1.0 cannot claim structured collaboration semantics")
            return self
        if self.purpose is not MessagePurpose.DISCUSSION and (
            not self.scope or not self.evidence_references
        ):
            raise ValueError(
                "structured collaboration message requires bounded scope and attributable evidence"
            )
        return self


class DecisionContextElement(ConversationModel):
    statement: str = Field(min_length=1, max_length=4_096)
    provenance_reference: str = Field(min_length=1, max_length=1_024)


class DecisionContext(ConversationModel):
    question: str = Field(min_length=3, max_length=8_192)
    objective_reference: str = Field(min_length=1, max_length=1_024)
    effective_policy_reference: str = Field(min_length=1, max_length=1_024)
    requirements: tuple[DecisionContextElement, ...] = Field(min_length=1)
    repository_evidence: tuple[DecisionContextElement, ...] = Field(min_length=1)
    constraints: tuple[DecisionContextElement, ...] = Field(min_length=1)
    declared_preferences: tuple[DecisionContextElement, ...] = Field(min_length=1)
    risks: tuple[DecisionContextElement, ...] = Field(min_length=1)
    material_unknowns: tuple[DecisionContextElement, ...] = Field(min_length=1)

    @property
    def provenance_references(self) -> frozenset[str]:
        groups = (
            self.requirements,
            self.repository_evidence,
            self.constraints,
            self.declared_preferences,
            self.risks,
            self.material_unknowns,
        )
        return frozenset(
            {
                self.objective_reference,
                self.effective_policy_reference,
                *(item.provenance_reference for group in groups for item in group),
            }
        )


class DecisionEvidenceClaim(ConversationModel):
    claim: str = Field(min_length=1, max_length=8_192)
    classification: DecisionEvidenceClass
    source_reference: str | None = Field(default=None, min_length=1, max_length=1_024)

    @model_validator(mode="after")
    def verified_claim_has_a_source(self) -> DecisionEvidenceClaim:
        if self.classification is DecisionEvidenceClass.VERIFIED and self.source_reference is None:
            raise ValueError("verified decision evidence requires an attributable source")
        return self


class DecisionCriterion(ConversationModel):
    criterion_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    description: str = Field(min_length=3, max_length=4_096)
    provenance_references: tuple[str, ...] = Field(min_length=1)
    weight: float | None = Field(default=None, gt=0)


class DecisionCriterionAssessment(ConversationModel):
    criterion_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    assessment: str = Field(min_length=1, max_length=4_096)
    evidence_references: tuple[str, ...] = Field(min_length=1)


class DecisionAlternative(ConversationModel):
    option_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    description: str = Field(min_length=3, max_length=4_096)
    credible: bool
    rejection_reason: str | None = Field(default=None, min_length=3, max_length=4_096)
    assessments: tuple[DecisionCriterionAssessment, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def rejected_candidate_explains_why_it_is_not_credible(self) -> DecisionAlternative:
        if not self.credible and self.rejection_reason is None:
            raise ValueError("non-credible decision alternative requires a rejection reason")
        if self.credible and self.rejection_reason is not None:
            raise ValueError("credible decision alternative cannot carry a rejection reason")
        return self


class DecisionRecommendation(ConversationModel):
    recommended_option_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{1,63}$")
    evidence_insufficient: bool = False
    rationale: str = Field(min_length=3, max_length=8_192)
    tradeoffs: tuple[str, ...] = Field(min_length=1)
    risks: tuple[str, ...] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    confidence_basis: str = Field(min_length=3, max_length=4_096)
    unresolved_questions: tuple[str, ...] = Field(min_length=1)
    expected_consequences: tuple[str, ...] = Field(min_length=1)
    reversal_or_migration: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def recommends_exactly_one_option_or_declares_insufficiency(
        self,
    ) -> DecisionRecommendation:
        if self.evidence_insufficient == (self.recommended_option_id is not None):
            raise ValueError(
                "decision result must recommend one option or declare evidence insufficient"
            )
        return self


class DecisionValidation(ConversationModel):
    validation_type: str = Field(min_length=2, max_length=256)
    planned_evidence: tuple[str, ...] = Field(min_length=1)
    status: DecisionValidationStatus
    evaluator_identity: str | None = Field(default=None, min_length=1, max_length=256)
    evidence_references: tuple[str, ...] = ()
    findings: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def settled_validation_has_evaluator_and_evidence(self) -> DecisionValidation:
        if self.status is DecisionValidationStatus.PENDING:
            if self.evaluator_identity is not None or self.evidence_references:
                raise ValueError("pending validation cannot claim evaluator evidence")
        elif self.evaluator_identity is None or not self.evidence_references:
            raise ValueError("settled validation requires evaluator and evidence")
        return self


class MissionDecision(ConversationModel):
    schema_version: Literal["1.0", "1.1"] = "1.0"
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
    decision_status: DecisionStatus | None = None
    producer_identity: str | None = Field(default=None, min_length=1, max_length=256)
    deciding_identity: str | None = Field(default=None, min_length=1, max_length=256)
    changes_durable_authority: bool | None = None
    context: DecisionContext | None = None
    evidence: tuple[DecisionEvidenceClaim, ...] = ()
    criteria: tuple[DecisionCriterion, ...] = ()
    alternatives: tuple[DecisionAlternative, ...] = ()
    alternatives_search: str | None = Field(default=None, min_length=3, max_length=8_192)
    recommendation: DecisionRecommendation | None = None
    validation: DecisionValidation | None = None
    explanation_preference: DecisionExplanationPreference | None = None
    supersedes_decision_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def consequential_decision_contract_is_complete(self) -> MissionDecision:
        structured = (
            self.decision_status,
            self.producer_identity,
            self.changes_durable_authority,
            self.context,
            self.recommendation,
            self.validation,
        )
        if self.schema_version == "1.0":
            if any(item is not None for item in structured) or any(
                (
                    self.evidence,
                    self.criteria,
                    self.alternatives,
                    self.alternatives_search is not None,
                    self.deciding_identity is not None,
                    self.supersedes_decision_id is not None,
                    self.explanation_preference is not None,
                )
            ):
                raise ValueError("decision 1.0 cannot carry partial 1.1 decision fields")
            return self
        if any(item is None for item in structured) or not all(
            (self.evidence, self.criteria, self.alternatives)
        ):
            raise ValueError("decision 1.1 requires complete consequential decision evidence")
        assert self.decision_status is not None
        assert self.producer_identity is not None
        assert self.context is not None
        assert self.recommendation is not None
        assert self.validation is not None
        if self.disposition != self.decision_status.value:
            raise ValueError("decision disposition must match its structured status")
        if self.decision_status in {DecisionStatus.STAGED, DecisionStatus.INCONCLUSIVE}:
            if self.actor_id != self.producer_identity:
                raise ValueError("staged decision actor must be its producing identity")
            if self.deciding_identity is not None or self.supersedes_decision_id is not None:
                raise ValueError("unsettled decision cannot claim a deciding identity or lineage")
        else:
            if self.deciding_identity != self.actor_id or self.supersedes_decision_id is None:
                raise ValueError("settled decision requires deciding actor and staged lineage")
        if self.decision_status is DecisionStatus.INCONCLUSIVE:
            if not self.recommendation.evidence_insufficient:
                raise ValueError("inconclusive decision must declare insufficient evidence")
        elif self.recommendation.evidence_insufficient:
            raise ValueError("only an inconclusive decision may declare insufficient evidence")
        if (
            self.validation.evaluator_identity is not None
            and self.validation.evaluator_identity == self.producer_identity
        ):
            raise ValueError("decision producer cannot evaluate its own recommendation")
        if self.decision_status is DecisionStatus.ACCEPTED and (
            self.validation.status is not DecisionValidationStatus.PASSED
        ):
            raise ValueError("accepted decision requires passed independent validation")
        criterion_ids = tuple(item.criterion_id for item in self.criteria)
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("decision criteria must have unique identities")
        option_ids = tuple(item.option_id for item in self.alternatives)
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("decision alternatives must have unique identities")
        expected_criteria = set(criterion_ids)
        for alternative in self.alternatives:
            assessment_ids = tuple(item.criterion_id for item in alternative.assessments)
            if (
                len(assessment_ids) != len(set(assessment_ids))
                or set(assessment_ids) != expected_criteria
            ):
                raise ValueError("every alternative must use the same declared criteria exactly")
        credible = tuple(item for item in self.alternatives if item.credible)
        if len(credible) == 1 and (
            self.alternatives_search is None
            or not any(not item.credible for item in self.alternatives)
        ):
            raise ValueError(
                "single credible option requires documented search and rejected candidates"
            )
        if len(credible) > 1 and self.alternatives_search is None:
            raise ValueError("multiple credible alternatives require documented search")
        selected = self.recommendation.recommended_option_id
        if selected is not None and selected not in {item.option_id for item in credible}:
            raise ValueError("recommendation must select a credible compared alternative")
        if not credible and not self.recommendation.evidence_insufficient:
            raise ValueError("absence of a credible option requires an inconclusive result")
        allowed_provenance = self.context.provenance_references | frozenset(
            {
                claim.source_reference
                for claim in self.evidence
                if claim.source_reference is not None
            }
        )
        if any(
            reference not in allowed_provenance
            for criterion in self.criteria
            for reference in criterion.provenance_references
        ):
            raise ValueError("decision criterion is not traceable to declared context")
        if any(
            claim.classification is DecisionEvidenceClass.VERIFIED
            and claim.source_reference not in self.evidence_references
            for claim in self.evidence
        ):
            raise ValueError("verified decision evidence source is absent from evidence lineage")
        return self


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
        recommendation_identities = [item.identity_id for item in self.recommendations]
        if len(recommendation_identities) != 2 or set(recommendation_identities) != {
            "PM",
            "CTO",
        }:
            raise ValueError("mission escalation requires distinct PM and CTO recommendations")
        if any(item.recommended_option_id not in option_ids for item in self.recommendations):
            raise ValueError("executive recommendation must reference an available option")
        if self.state is EscalationState.OPEN and self.answer_intervention_id is not None:
            raise ValueError("open escalation cannot already reference an answer")
        if self.state in {EscalationState.ANSWERED, EscalationState.RESOLVED} and (
            self.answer_intervention_id is None
        ):
            raise ValueError("answered escalation requires its intervention identity")
        if set(self.blocked_scope).intersection(self.independent_work_continuing):
            raise ValueError("blocked escalation scope cannot also be declared independent work")
        if self.deadline is not None and self.deadline <= self.created_at:
            raise ValueError("escalation deadline must follow its creation time")
        if self.updated_at < self.created_at:
            raise ValueError("escalation update time cannot precede its creation time")
        return self


class MissionIntervention(ConversationModel):
    schema_version: Literal["1.0", "1.1"] = "1.0"
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
    selected_option_id: str | None = Field(default=None, min_length=1, max_length=128)
    effect: str = Field(min_length=3, max_length=4_096)
    resulting_target_state: InterventionResultState | None = None
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
            if self.schema_version == "1.1" and self.selected_option_id is None:
                raise ValueError("escalation answer must select one available option")
        elif self.escalation_id is not None or self.selected_option_id is not None:
            raise ValueError("only an escalation answer may carry escalation selection fields")
        expected_target_state = {
            InterventionKind.COMMENT: InterventionResultState.UNCHANGED,
            InterventionKind.ANSWER_ESCALATION: InterventionResultState.ANSWERED,
            InterventionKind.ACCEPT_PROPOSAL: InterventionResultState.ACCEPTED,
            InterventionKind.REJECT_PROPOSAL: InterventionResultState.REJECTED,
            InterventionKind.SUSPEND: InterventionResultState.PAUSED,
            InterventionKind.RESUME: InterventionResultState.ACTIVE,
            InterventionKind.REQUEST_REASSIGNMENT: (InterventionResultState.REASSIGNMENT_REQUESTED),
            InterventionKind.CONFIRM_REASSIGNMENT: (InterventionResultState.REASSIGNMENT_CONFIRMED),
            InterventionKind.STOP: InterventionResultState.CANCELLED,
            InterventionKind.ACCEPT_RISK: InterventionResultState.RISK_ACCEPTED,
        }[self.kind]
        if self.schema_version == "1.0":
            if self.resulting_target_state is not None or self.selected_option_id is not None:
                raise ValueError("intervention 1.0 cannot carry 1.1 settlement fields")
        elif self.resulting_target_state is not expected_target_state:
            raise ValueError("intervention resulting target state does not match its kind")
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
