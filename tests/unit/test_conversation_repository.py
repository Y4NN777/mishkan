from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from mishkan.conversations import (
    ChannelClass,
    ConversationChannel,
    ConversationMessage,
    DecisionAlternative,
    DecisionContext,
    DecisionContextElement,
    DecisionCriterion,
    DecisionCriterionAssessment,
    DecisionEvidenceClaim,
    DecisionEvidenceClass,
    DecisionExplanationDepth,
    DecisionExplanationPreference,
    DecisionRecommendation,
    DecisionStatus,
    DecisionValidation,
    DecisionValidationStatus,
    EscalationOption,
    ExecutiveRecommendation,
    InterventionKind,
    InterventionTargetKind,
    MessagePurpose,
    MissionDecision,
    MissionEscalation,
    MissionIntervention,
    SQLiteConversationRepository,
)
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.identity import new_id
from mishkan.domain.time import utc_now
from mishkan.missions import (
    CrewAssignmentKind,
    CrewSelectionEvidence,
    ExecutiveConfirmation,
    MissionBrief,
    MissionBriefStatus,
    MissionCrewMember,
    MissionCrewRevision,
    MissionEnvironmentIntent,
    MissionOrigin,
    MissionOriginKind,
    MissionRecord,
    MissionResourceLimit,
    MissionState,
    MissionTaskAssignment,
    SQLiteMissionRepository,
)
from mishkan.organization import load_canonical_organization
from mishkan.persistence.migration import SchemaManager


def _confirmation(identity_id: Literal["PM", "CTO"]) -> ExecutiveConfirmation:
    return ExecutiveConfirmation(
        identity_id=identity_id,
        disposition="confirmed",
        rationale=f"{identity_id} confirms the mission coverage",
        evidence_references=(f"evidence:{identity_id.lower()}",),
        coverage=(
            ("product", "developer-experience", "composition")
            if identity_id == "PM"
            else ("technical", "platform", "security", "quality", "operability")
        ),
    )


def _setup(
    tmp_path: Path,
) -> tuple[SQLiteMissionRepository, SQLiteConversationRepository, MissionRecord]:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    missions = SQLiteMissionRepository(database)
    organization = load_canonical_organization()
    missions.record_organization(organization)
    mission = missions.create_mission(
        MissionRecord(
            origin=MissionOrigin(
                kind=MissionOriginKind.CEO,
                actor_id="CEO",
                objective="Recover a blocked product mission",
            ),
            organization_id=organization.organization_id,
            organization_version=organization.organization_version,
        )
    )
    brief = MissionBrief(
        mission_id=mission.mission_id,
        version=1,
        organization_id=mission.organization_id,
        organization_version=mission.organization_version,
        status=MissionBriefStatus.CONFIRMED,
        objective=mission.origin.objective,
        problem="A product decision blocks only one dependent task",
        desired_outcome="Resolve it without freezing independent work",
        scope=("task:blocked",),
        exclusions=("task:independent",),
        acceptance_criteria=("independent work remains eligible",),
        constraints=("preserve accepted evidence",),
        risks=("unnecessary mission-wide pause",),
        authority_scope=("mission:governance",),
        proposed_crew=("PM", "CTO"),
        evidence_requirements=("decision evidence",),
        escalation_conditions=("PM and CTO remain in disagreement",),
        environment_intent=MissionEnvironmentIntent(environment_dependent=False),
        pm_confirmation=_confirmation("PM"),
        cto_confirmation=_confirmation("CTO"),
    )
    missions.record_brief(brief, expected_revision=mission.revision)
    return (
        missions,
        SQLiteConversationRepository(database),
        missions.mission(str(mission.mission_id)),
    )


def _mission_channel(mission: MissionRecord) -> ConversationChannel:
    return ConversationChannel(
        channel_class=ChannelClass.MISSION,
        title="Account recovery mission",
        participants=("CEO", "PM", "CTO"),
        mission_id=mission.mission_id,
        created_by="PM",
    )


def _escalation(mission: MissionRecord, channel: ConversationChannel) -> MissionEscalation:
    return MissionEscalation(
        mission_id=mission.mission_id,
        conversation_id=channel.conversation_id,
        raised_by="PM",
        blocked_scope=("task:blocked",),
        decision_required="Choose whether to accept the compatibility risk",
        reason="PM and CTO recommendations remain different",
        options=(
            EscalationOption(
                option_id="accept",
                description="Accept the bounded compatibility risk",
                consequences=("blocked task can continue",),
                risks=("legacy clients may behave differently",),
            ),
            EscalationOption(
                option_id="reject",
                description="Reject the risk and redesign the change",
                consequences=("blocked task remains paused",),
                risks=("delivery is delayed",),
            ),
        ),
        recommendations=(
            ExecutiveRecommendation(
                identity_id="PM",
                recommended_option_id="accept",
                rationale="The user impact is bounded",
                evidence_references=("evidence:product-analysis",),
            ),
            ExecutiveRecommendation(
                identity_id="CTO",
                recommended_option_id="reject",
                rationale="Compatibility evidence is incomplete",
                evidence_references=("evidence:technical-review",),
            ),
        ),
        uncertainty=("legacy client population",),
        independent_work_continuing=("task:independent",),
        evidence_references=("evidence:disagreement-record",),
    )


def _add_governed_task(
    missions: SQLiteMissionRepository,
    mission: MissionRecord,
) -> tuple[MissionRecord, MissionTaskAssignment]:
    original = missions.brief(str(mission.mission_id))
    revised = original.model_copy(
        update={
            "brief_id": new_id(),
            "version": 2,
            "proposed_crew": (
                "Backend_Service_Engineer",
                "Product_Functional_Evaluator",
                "Technical_Change_Reporter",
            ),
            "pm_confirmation": _confirmation("PM"),
            "cto_confirmation": _confirmation("CTO"),
        }
    )
    missions.record_brief(revised, expected_revision=mission.revision)
    assert revised.pm_confirmation is not None
    assert revised.cto_confirmation is not None

    def member(identity_id: str, kind: CrewAssignmentKind) -> MissionCrewMember:
        return MissionCrewMember(
            identity_id=identity_id,
            assignment_kind=kind,
            responsibility=f"Own {kind.value} responsibility for the governed task",
            selection_evidence=CrewSelectionEvidence(
                project_references=("project:conversation-test",),
                competence_references=(f"profile:{identity_id}:competence",),
                availability_references=(f"profile:{identity_id}:availability",),
                conflict_assessment="No responsibility conflict is present",
                risk_coverage=("mission control",),
                independence_references=(f"profile:{identity_id}:independence",),
            ),
        )

    crew = MissionCrewRevision(
        mission_id=mission.mission_id,
        version=1,
        organization_id=mission.organization_id,
        organization_version=mission.organization_version,
        brief_version=revised.version,
        mission_lead_id="Backend_Service_Engineer",
        members=(
            member("Backend_Service_Engineer", CrewAssignmentKind.PRODUCTION),
            member("Product_Functional_Evaluator", CrewAssignmentKind.EVALUATION),
            member("Technical_Change_Reporter", CrewAssignmentKind.REPORTING),
        ),
        pm_composition_confirmation_id=revised.pm_confirmation.confirmation_id,
        cto_coverage_confirmation_id=revised.cto_confirmation.confirmation_id,
        revision_reason="Create an explicitly governed task-control fixture",
    )
    current = missions.mission(str(mission.mission_id))
    missions.record_crew(crew, expected_revision=current.revision)
    assignment = MissionTaskAssignment(
        mission_id=mission.mission_id,
        crew_version=crew.version,
        task_id="blocked-task",
        accountable_owner="Backend_Service_Engineer",
        assignment_kind=CrewAssignmentKind.PRODUCTION,
        expected_result="A bounded task result",
        completion_criteria=("result is reviewable",),
        authority_scope=("task:blocked-task",),
        exact_tools=("file.read",),
        path_scopes=("repository:test",),
        limits=(MissionResourceLimit(name="wall_time", value=600, unit="seconds"),),
        required_evidence=("artifact:result",),
    )
    missions.record_assignment(assignment)
    return missions.mission(str(mission.mission_id)), assignment


def test_channel_contracts_keep_direct_and_executive_authority_explicit() -> None:
    with pytest.raises(ValidationError):
        ConversationChannel(
            channel_class=ChannelClass.EXECUTIVE,
            title="Incomplete executive channel",
            participants=("PM", "CTO"),
            created_by="PM",
        )


def test_intervention_contract_rejects_kind_target_and_state_mismatches() -> None:
    with pytest.raises(ValidationError, match="does not support"):
        MissionIntervention(
            mission_id=new_id(),
            conversation_id=new_id(),
            actor_id="CEO",
            kind=InterventionKind.ACCEPT_RISK,
            target_kind=InterventionTargetKind.MISSION,
            target_id="mission:one",
            reason="Attempt an invalid target combination",
            scope=("mission:one",),
            confirmation="Confirm the malformed request",
            authority_reference="authority:ceo",
            evidence_references=("evidence:risk",),
            effect="Would incorrectly change mission governance",
        )
    with pytest.raises(ValidationError):
        ConversationChannel(
            channel_class=ChannelClass.DIRECT,
            title="Unauthorized direct channel",
            participants=("PM", "CTO"),
            created_by="PM",
        )


def test_escalation_requires_distinct_pm_and_cto_recommendations() -> None:
    mission = MissionRecord(
        origin=MissionOrigin(
            kind=MissionOriginKind.CEO,
            actor_id="CEO",
            objective="Resolve an executive disagreement",
        ),
        organization_id="mishkan",
        organization_version="1",
    )
    channel = _mission_channel(mission)
    escalation = _escalation(mission, channel)

    with pytest.raises(ValidationError, match="distinct PM and CTO recommendations"):
        MissionEscalation.model_validate(
            escalation.model_copy(
                update={"recommendations": (escalation.recommendations[0],)}
            ).model_dump(mode="json")
        )


def test_messages_are_durable_records_and_never_implicit_commands(tmp_path: Path) -> None:
    missions, conversations, mission = _setup(tmp_path)
    channel = conversations.create_channel(_mission_channel(mission))
    message = ConversationMessage(
        conversation_id=channel.conversation_id,
        author_identity="CEO",
        body="I prefer option A, but this message does not authorize it.",
        evidence_references=("evidence:ceo-context",),
    )

    assert conversations.post_message(message) == message
    assert conversations.post_message(message) == message
    assert conversations.messages(str(channel.conversation_id)) == (message,)
    assert missions.mission(str(mission.mission_id)).revision == mission.revision


def test_collaboration_messages_are_typed_bounded_and_non_mutating(tmp_path: Path) -> None:
    missions, conversations, mission = _setup(tmp_path)
    channel = conversations.create_channel(_mission_channel(mission))
    purposes = (
        MessagePurpose.COLLABORATION,
        MessagePurpose.CONSULTATION,
        MessagePurpose.HANDOFF_CONTEXT,
        MessagePurpose.REVIEW,
        MessagePurpose.EVIDENCE_CHALLENGE,
    )
    messages = tuple(
        ConversationMessage(
            schema_version="1.1",
            conversation_id=channel.conversation_id,
            author_identity="PM" if index % 2 == 0 else "CTO",
            purpose=purpose,
            body=f"Record the bounded {purpose.value} context without changing authority.",
            scope=("task:blocked",),
            evidence_references=(f"evidence:{purpose.value}",),
        )
        for index, purpose in enumerate(purposes)
    )

    for message in messages:
        conversations.post_message(message)

    assert conversations.messages(str(channel.conversation_id)) == messages
    assert missions.mission(str(mission.mission_id)).revision == mission.revision

    with pytest.raises(ValidationError, match="cannot claim structured collaboration semantics"):
        ConversationMessage(
            conversation_id=channel.conversation_id,
            author_identity="PM",
            purpose=MessagePurpose.CONSULTATION,
            body="Invalid unversioned consultation.",
            scope=("task:blocked",),
            evidence_references=("evidence:consultation",),
        )
    with pytest.raises(ValidationError, match="bounded scope and attributable evidence"):
        ConversationMessage(
            schema_version="1.1",
            conversation_id=channel.conversation_id,
            author_identity="CTO",
            purpose=MessagePurpose.EVIDENCE_CHALLENGE,
            body="Unattributed challenge.",
        )


def test_executive_conversation_and_message_survive_repository_restart(tmp_path: Path) -> None:
    _missions, conversations, _mission = _setup(tmp_path)
    channel = conversations.create_channel(
        ConversationChannel(
            channel_class=ChannelClass.EXECUTIVE,
            title="CEO PM CTO executive conversation",
            participants=("CEO", "PM", "CTO"),
            created_by="CEO",
        )
    )
    message = conversations.post_message(
        ConversationMessage(
            conversation_id=channel.conversation_id,
            author_identity="CEO",
            body="Preserve this executive context across disconnected clients.",
            evidence_references=("evidence:executive-context",),
        )
    )

    reopened = SQLiteConversationRepository(tmp_path / "mishkan.db")

    assert reopened.channel(str(channel.conversation_id)) == channel
    assert reopened.messages(str(channel.conversation_id)) == (message,)


def test_channels_require_known_people_and_real_organization_branches(tmp_path: Path) -> None:
    _missions, conversations, _mission = _setup(tmp_path)
    unknown_person = ConversationChannel(
        channel_class=ChannelClass.DIRECT,
        title="Unknown direct participant",
        participants=("PM", "Invented_Agent"),
        direct_authorization_reference="authority:pm-direct",
        created_by="PM",
    )
    unknown_branch = ConversationChannel(
        channel_class=ChannelClass.BRANCH,
        title="Invented branch discussion",
        participants=("PM", "CTO"),
        branch_id="invented-branch",
        created_by="PM",
    )

    with pytest.raises(MishkanError, match="unknown professional identities"):
        conversations.create_channel(unknown_person)
    with pytest.raises(MishkanError, match="unknown organization branch"):
        conversations.create_channel(unknown_branch)


def test_disagreement_preserves_independent_work_and_answer_is_attributed(
    tmp_path: Path,
) -> None:
    missions, conversations, mission = _setup(tmp_path)
    channel = conversations.create_channel(_mission_channel(mission))
    escalation = conversations.create_escalation(_escalation(mission, channel))
    intervention = MissionIntervention(
        mission_id=mission.mission_id,
        conversation_id=channel.conversation_id,
        actor_id="CEO",
        kind=InterventionKind.ANSWER_ESCALATION,
        target_kind=InterventionTargetKind.ESCALATION,
        target_id=str(escalation.escalation_id),
        reason="The bounded evidence supports accepting this risk",
        scope=("task:blocked",),
        confirmation="Accept option A for the blocked task only",
        authority_reference="authority:ceo",
        evidence_references=("evidence:ceo-decision",),
        escalation_id=escalation.escalation_id,
        effect="Resolve the escalation without changing unrelated mission work",
    )

    conversations.apply_intervention(intervention, expected_revision=mission.revision)

    answered = conversations.escalations(str(mission.mission_id))[0]
    durable = missions.mission(str(mission.mission_id))
    assert answered.state.value == "answered"
    assert answered.answer_intervention_id == intervention.intervention_id
    assert answered.independent_work_continuing == ("task:independent",)
    assert durable.state is mission.state
    assert durable.revision == mission.revision + 1


def test_mission_pause_and_resume_are_explicit_revision_checked_interventions(
    tmp_path: Path,
) -> None:
    missions, conversations, mission = _setup(tmp_path)
    channel = conversations.create_channel(_mission_channel(mission))
    pause = MissionIntervention(
        mission_id=mission.mission_id,
        conversation_id=channel.conversation_id,
        actor_id="CEO",
        kind=InterventionKind.SUSPEND,
        target_kind=InterventionTargetKind.MISSION,
        target_id=str(mission.mission_id),
        reason="Pause while the material risk is independently evaluated",
        scope=(f"mission:{mission.mission_id}",),
        confirmation="Suspend the complete mission",
        authority_reference="authority:ceo",
        evidence_references=("evidence:risk-finding",),
        effect="Stop new mission work until a governed resume",
        resulting_mission_state=MissionState.PAUSED,
    )
    conversations.apply_intervention(pause, expected_revision=mission.revision)
    paused = missions.mission(str(mission.mission_id))
    assert paused.state is MissionState.PAUSED

    resume = pause.model_copy(
        update={
            "intervention_id": new_id(),
            "kind": InterventionKind.RESUME,
            "reason": "Independent evaluation has settled the material risk",
            "confirmation": "Resume the complete mission",
            "effect": "Allow mission work to become eligible again",
            "evidence_references": ("evidence:risk-settlement",),
            "resulting_mission_state": MissionState.ACTIVE,
            "created_at": utc_now(),
        }
    )
    conversations.apply_intervention(resume, expected_revision=paused.revision)
    assert missions.mission(str(mission.mission_id)).state is MissionState.ACTIVE

    stale_stop = pause.model_copy(
        update={
            "intervention_id": new_id(),
            "kind": InterventionKind.STOP,
            "reason": "Attempt a stale mission stop",
            "confirmation": "Stop the complete mission",
            "effect": "Cancel the mission",
            "resulting_mission_state": MissionState.CANCELLED,
            "created_at": utc_now(),
        }
    )
    with pytest.raises(MishkanError) as stale:
        conversations.apply_intervention(stale_stop, expected_revision=paused.revision)
    assert stale.value.envelope.code is ErrorCode.REVISION_MISMATCH


def test_stopped_task_cannot_be_resumed_and_unrelated_work_is_unchanged(
    tmp_path: Path,
) -> None:
    missions, conversations, mission = _setup(tmp_path)
    mission, assignment = _add_governed_task(missions, mission)
    channel = conversations.create_channel(_mission_channel(mission))
    suspend = MissionIntervention(
        mission_id=mission.mission_id,
        conversation_id=channel.conversation_id,
        actor_id="CEO",
        kind=InterventionKind.SUSPEND,
        target_kind=InterventionTargetKind.TASK,
        target_id=assignment.task_id,
        reason="Pause only this bounded task",
        scope=(f"task:{assignment.task_id}",),
        confirmation="Suspend this task without changing the mission state",
        authority_reference="authority:ceo",
        evidence_references=("evidence:task-risk",),
        effect="Prevent new execution claims for this task",
    )
    conversations.apply_intervention(suspend, expected_revision=mission.revision)
    current = missions.mission(str(mission.mission_id))
    resume = suspend.model_copy(
        update={
            "intervention_id": new_id(),
            "kind": InterventionKind.RESUME,
            "reason": "The bounded blocker has been resolved",
            "confirmation": "Resume only the suspended task",
            "effect": "Allow this task to become eligible again",
            "evidence_references": ("evidence:task-resolution",),
            "created_at": utc_now(),
        }
    )
    conversations.apply_intervention(resume, expected_revision=current.revision)
    current = missions.mission(str(mission.mission_id))
    stop = resume.model_copy(
        update={
            "intervention_id": new_id(),
            "kind": InterventionKind.STOP,
            "reason": "The task must terminate permanently",
            "confirmation": "Stop only this task",
            "effect": "Prevent any later claim or resume for this task",
            "evidence_references": ("evidence:stop-decision",),
            "created_at": utc_now(),
        }
    )
    conversations.apply_intervention(stop, expected_revision=current.revision)
    current = missions.mission(str(mission.mission_id))
    invalid_resume = resume.model_copy(
        update={"intervention_id": new_id(), "created_at": utc_now()}
    )

    with pytest.raises(MishkanError, match="stopped mission task"):
        conversations.apply_intervention(invalid_resume, expected_revision=current.revision)

    assert missions.mission(str(mission.mission_id)).state is mission.state


def test_mission_decisions_are_queryable_after_restart(tmp_path: Path) -> None:
    _missions, conversations, mission = _setup(tmp_path)
    channel = conversations.create_channel(_mission_channel(mission))
    decision = MissionDecision(
        mission_id=mission.mission_id,
        conversation_id=channel.conversation_id,
        actor_id="PM",
        subject="Mission scope",
        disposition="accepted",
        reason="The scope is within the approved product envelope",
        scope=("task:independent",),
        evidence_references=("evidence:scope-review",),
        authority_reference="authority:pm-product",
    )
    conversations.record_decision(decision)

    reopened = SQLiteConversationRepository(tmp_path / "mishkan.db")

    assert reopened.decisions(str(mission.mission_id)) == (decision,)


def _consequential_decision(
    mission: MissionRecord,
    channel: ConversationChannel,
) -> MissionDecision:
    context = DecisionContext(
        question="Which persistence option satisfies the mission constraints?",
        objective_reference="objective:mission",
        effective_policy_reference="policy:mission-v1",
        requirements=(
            DecisionContextElement(
                statement="Preserve transactional acceptance",
                provenance_reference="requirement:RUN-008",
            ),
        ),
        repository_evidence=(
            DecisionContextElement(
                statement="The current local authority uses SQLite WAL",
                provenance_reference="evidence:repository-inspection",
            ),
        ),
        constraints=(
            DecisionContextElement(
                statement="Local mode must remain self-contained",
                provenance_reference="constraint:local-mode",
            ),
        ),
        declared_preferences=(
            DecisionContextElement(
                statement="Prefer an OSS local-first dependency",
                provenance_reference="preference:oss-local",
            ),
        ),
        risks=(
            DecisionContextElement(
                statement="Concurrent writers could exceed the local profile",
                provenance_reference="risk:writer-contention",
            ),
        ),
        material_unknowns=(
            DecisionContextElement(
                statement="Peak writer concurrency is not measured yet",
                provenance_reference="unknown:peak-writers",
            ),
        ),
    )

    criteria = (
        DecisionCriterion(
            criterion_id="transactional-acceptance",
            description="Preserves atomic durable result acceptance",
            provenance_references=("requirement:RUN-008",),
        ),
        DecisionCriterion(
            criterion_id="writer-contention",
            description="Operates within measured concurrent writer demand",
            provenance_references=("risk:writer-contention", "unknown:peak-writers"),
        ),
    )

    def assessments(prefix: str) -> tuple[DecisionCriterionAssessment, ...]:
        return tuple(
            DecisionCriterionAssessment(
                criterion_id=criterion.criterion_id,
                assessment=f"{prefix}: {criterion.description}",
                evidence_references=("evidence:repository-inspection",),
            )
            for criterion in criteria
        )

    return MissionDecision(
        schema_version="1.1",
        mission_id=mission.mission_id,
        conversation_id=channel.conversation_id,
        actor_id="backend-engineer",
        producer_identity="backend-engineer",
        subject="Local persistence architecture",
        disposition="staged",
        decision_status=DecisionStatus.STAGED,
        reason="The recommendation is staged until its benchmark is independently evaluated",
        scope=("architecture:persistence",),
        evidence_references=("evidence:repository-inspection",),
        authority_reference="authority:architecture-decision",
        changes_durable_authority=True,
        context=context,
        evidence=(
            DecisionEvidenceClaim(
                claim="SQLite WAL is the current local metadata authority",
                classification=DecisionEvidenceClass.VERIFIED,
                source_reference="evidence:repository-inspection",
            ),
            DecisionEvidenceClaim(
                claim="Expected writer concurrency may remain low",
                classification=DecisionEvidenceClass.ASSUMPTION,
            ),
            DecisionEvidenceClaim(
                claim="The engineer prefers an OSS local-first dependency",
                classification=DecisionEvidenceClass.ENGINEER_PREFERENCE,
                source_reference="preference:oss-local",
            ),
        ),
        criteria=criteria,
        alternatives=(
            DecisionAlternative(
                option_id="sqlite-wal",
                description="Retain SQLite with WAL for local mode",
                credible=True,
                assessments=assessments("SQLite evidence"),
            ),
            DecisionAlternative(
                option_id="postgres-local",
                description="Require PostgreSQL for local mode",
                credible=True,
                assessments=assessments("PostgreSQL evidence"),
            ),
        ),
        alternatives_search="Compared the current store with the distributed-mode database",
        recommendation=DecisionRecommendation(
            recommended_option_id="sqlite-wal",
            rationale="It preserves the local operating constraint with the existing authority",
            tradeoffs=("It supports fewer concurrent writers than PostgreSQL",),
            risks=("Unmeasured writer contention could invalidate the choice",),
            confidence=0.72,
            confidence_basis="Repository evidence exists but peak concurrency remains unknown",
            unresolved_questions=("What is the peak concurrent writer count?",),
            expected_consequences=("Local setup remains self-contained",),
            reversal_or_migration=("Migrate metadata through explicit Alembic revisions",),
        ),
        validation=DecisionValidation(
            validation_type="focused write-contention benchmark",
            planned_evidence=("Benchmark concurrent local writers",),
            status=DecisionValidationStatus.PENDING,
            findings=("The independent benchmark has not run yet",),
        ),
        explanation_preference=DecisionExplanationPreference(
            requested_by="engineer:y4nn777",
            depth=DecisionExplanationDepth.DEEP,
            focus_areas=("migration", "operational-risk"),
            request_reference="conversation:engineer-explanation-request",
        ),
    )


def _ceo_intervention(
    mission: MissionRecord,
    channel: ConversationChannel,
    *,
    kind: InterventionKind,
    target_kind: InterventionTargetKind,
    target_id: str,
) -> MissionIntervention:
    return MissionIntervention(
        mission_id=mission.mission_id,
        conversation_id=channel.conversation_id,
        actor_id="CEO",
        kind=kind,
        target_kind=target_kind,
        target_id=target_id,
        reason=f"Apply the explicit CEO {kind.value} decision",
        scope=(f"{target_kind.value}:{target_id}",),
        confirmation=f"Confirm the exact {kind.value} intervention",
        authority_reference="authority:ceo",
        evidence_references=("evidence:ceo-intervention",),
        effect=f"Record the governed {kind.value} disposition",
    )


def test_proposal_intervention_requires_and_settles_one_durable_proposal(
    tmp_path: Path,
) -> None:
    missions, conversations, mission = _setup(tmp_path)
    channel = conversations.create_channel(_mission_channel(mission))
    staged = _consequential_decision(mission, channel)
    conversations.record_decision(staged)
    accepted = _ceo_intervention(
        mission,
        channel,
        kind=InterventionKind.ACCEPT_PROPOSAL,
        target_kind=InterventionTargetKind.PROPOSAL,
        target_id=str(staged.decision_id),
    )

    assert (
        conversations.apply_intervention(accepted, expected_revision=mission.revision) == accepted
    )
    current = missions.mission(str(mission.mission_id))
    rejected = _ceo_intervention(
        current,
        channel,
        kind=InterventionKind.REJECT_PROPOSAL,
        target_kind=InterventionTargetKind.PROPOSAL,
        target_id=str(staged.decision_id),
    )
    with pytest.raises(MishkanError, match="already has a durable CEO disposition") as duplicate:
        conversations.apply_intervention(rejected, expected_revision=current.revision)
    assert duplicate.value.envelope.code is ErrorCode.DUPLICATE_RESULT

    missing = _ceo_intervention(
        current,
        channel,
        kind=InterventionKind.ACCEPT_PROPOSAL,
        target_kind=InterventionTargetKind.PROPOSAL,
        target_id=str(new_id()),
    )
    with pytest.raises(MishkanError, match="proposal is absent"):
        conversations.apply_intervention(missing, expected_revision=current.revision)


def test_risk_acceptance_targets_existing_durable_mission_risk(tmp_path: Path) -> None:
    missions, conversations, mission = _setup(tmp_path)
    channel = conversations.create_channel(_mission_channel(mission))
    accepted = _ceo_intervention(
        mission,
        channel,
        kind=InterventionKind.ACCEPT_RISK,
        target_kind=InterventionTargetKind.RISK,
        target_id="unnecessary mission-wide pause",
    )

    conversations.apply_intervention(accepted, expected_revision=mission.revision)
    current = missions.mission(str(mission.mission_id))
    unknown = _ceo_intervention(
        current,
        channel,
        kind=InterventionKind.ACCEPT_RISK,
        target_kind=InterventionTargetKind.RISK,
        target_id="invented risk without durable evidence",
    )

    with pytest.raises(MishkanError, match="not present in durable mission evidence"):
        conversations.apply_intervention(unknown, expected_revision=current.revision)


def test_reassignment_confirmation_requires_a_pending_ceo_request(tmp_path: Path) -> None:
    missions, conversations, mission = _setup(tmp_path)
    mission, assignment = _add_governed_task(missions, mission)
    channel = conversations.create_channel(_mission_channel(mission))
    confirmation = _ceo_intervention(
        mission,
        channel,
        kind=InterventionKind.CONFIRM_REASSIGNMENT,
        target_kind=InterventionTargetKind.ASSIGNMENT,
        target_id=str(assignment.assignment_id),
    )
    with pytest.raises(MishkanError, match="requires a pending CEO request"):
        conversations.apply_intervention(confirmation, expected_revision=mission.revision)

    request = _ceo_intervention(
        mission,
        channel,
        kind=InterventionKind.REQUEST_REASSIGNMENT,
        target_kind=InterventionTargetKind.ASSIGNMENT,
        target_id=str(assignment.assignment_id),
    )
    conversations.apply_intervention(request, expected_revision=mission.revision)
    current = missions.mission(str(mission.mission_id))
    confirmation = _ceo_intervention(
        current,
        channel,
        kind=InterventionKind.CONFIRM_REASSIGNMENT,
        target_kind=InterventionTargetKind.ASSIGNMENT,
        target_id=str(assignment.assignment_id),
    )
    assert (
        conversations.apply_intervention(confirmation, expected_revision=current.revision)
        == confirmation
    )


def test_consequential_decision_requires_complete_context_and_independent_validation(
    tmp_path: Path,
) -> None:
    _missions, conversations, mission = _setup(tmp_path)
    channel = conversations.create_channel(_mission_channel(mission))
    staged = _consequential_decision(mission, channel)

    assert staged.explanation_preference is not None
    assert staged.explanation_preference.depth is DecisionExplanationDepth.DEEP

    with pytest.raises(ValidationError, match="complete consequential decision evidence"):
        MissionDecision(
            schema_version="1.1",
            mission_id=mission.mission_id,
            conversation_id=channel.conversation_id,
            actor_id="backend-engineer",
            subject="Incomplete architecture choice",
            disposition="staged",
            reason="Context was omitted",
            scope=("architecture:persistence",),
            evidence_references=("evidence:repository-inspection",),
            authority_reference="authority:architecture-decision",
        )

    with pytest.raises(ValidationError, match="producer cannot evaluate"):
        assert staged.validation is not None
        MissionDecision.model_validate(
            staged.model_copy(
                update={
                    "validation": staged.validation.model_copy(
                        update={
                            "status": DecisionValidationStatus.PASSED,
                            "evaluator_identity": staged.producer_identity,
                            "evidence_references": ("evidence:benchmark",),
                        }
                    )
                }
            ).model_dump(mode="json")
        )


def test_consequential_decision_rejects_false_evidence_and_unfair_comparison(
    tmp_path: Path,
) -> None:
    _missions, conversations, mission = _setup(tmp_path)
    channel = conversations.create_channel(_mission_channel(mission))
    staged = _consequential_decision(mission, channel)

    payload = staged.model_dump(mode="json")
    payload["evidence"][0]["source_reference"] = "evidence:unsupported-claim"
    with pytest.raises(ValidationError, match="absent from evidence lineage"):
        MissionDecision.model_validate(payload)

    payload = staged.model_dump(mode="json")
    payload["alternatives"][1]["assessments"] = payload["alternatives"][1]["assessments"][:1]
    with pytest.raises(ValidationError, match="same declared criteria exactly"):
        MissionDecision.model_validate(payload)

    payload = staged.model_dump(mode="json")
    payload["alternatives"] = payload["alternatives"][:1]
    payload["alternatives_search"] = None
    with pytest.raises(ValidationError, match="single credible option"):
        MissionDecision.model_validate(payload)

    payload = staged.model_dump(mode="json")
    payload.update(
        {
            "actor_id": "CTO",
            "deciding_identity": "CTO",
            "disposition": "accepted",
            "decision_status": "accepted",
            "supersedes_decision_id": str(new_id()),
        }
    )
    with pytest.raises(ValidationError, match="passed independent validation"):
        MissionDecision.model_validate(payload)


def test_consequential_decision_settlement_preserves_staged_lineage(
    tmp_path: Path,
) -> None:
    _missions, conversations, mission = _setup(tmp_path)
    channel = conversations.create_channel(_mission_channel(mission))
    staged = _consequential_decision(mission, channel)
    conversations.record_decision(staged)
    assert staged.validation is not None
    accepted = staged.model_copy(
        update={
            "decision_id": new_id(),
            "actor_id": "CTO",
            "deciding_identity": "CTO",
            "disposition": DecisionStatus.ACCEPTED.value,
            "decision_status": DecisionStatus.ACCEPTED,
            "supersedes_decision_id": staged.decision_id,
            "reason": "Independent evidence confirms the staged recommendation",
            "evidence_references": (
                *staged.evidence_references,
                "evidence:writer-benchmark",
            ),
            "validation": staged.validation.model_copy(
                update={
                    "status": DecisionValidationStatus.PASSED,
                    "evaluator_identity": "quality-engineer",
                    "evidence_references": ("evidence:writer-benchmark",),
                    "findings": ("The measured contention remains inside the local profile",),
                }
            ),
        }
    )
    accepted = MissionDecision.model_validate(accepted.model_dump(mode="json"))

    assert conversations.record_decision(accepted) == accepted
    assert conversations.decisions(str(mission.mission_id)) == (staged, accepted)

    conflicting = accepted.model_copy(update={"decision_id": new_id()})
    with pytest.raises(MishkanError, match="already has a durable disposition") as caught:
        conversations.record_decision(conflicting)
    assert caught.value.envelope.code is ErrorCode.DECISION_VALIDATION

    changed = accepted.model_copy(
        update={
            "decision_id": new_id(),
            "supersedes_decision_id": staged.decision_id,
            "subject": "A different architecture question",
        }
    )
    with pytest.raises(MishkanError, match="changes its staged recommendation"):
        conversations.record_decision(changed)
