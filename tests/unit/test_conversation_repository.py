from pathlib import Path

import pytest
from pydantic import ValidationError

from mishkan.conversations import (
    ChannelClass,
    ConversationChannel,
    ConversationMessage,
    EscalationOption,
    ExecutiveRecommendation,
    InterventionKind,
    InterventionTargetKind,
    MissionDecision,
    MissionEscalation,
    MissionIntervention,
    SQLiteConversationRepository,
)
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.identity import new_id
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


def _confirmation(identity_id: str) -> ExecutiveConfirmation:
    return ExecutiveConfirmation(
        identity_id=identity_id,
        disposition="confirmed",
        rationale=f"{identity_id} confirms the mission coverage",
        evidence_references=(f"evidence:{identity_id.lower()}",),
        coverage=("product" if identity_id == "PM" else "technical-security-quality",),
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
        }
    )
    conversations.apply_intervention(stop, expected_revision=current.revision)
    current = missions.mission(str(mission.mission_id))
    invalid_resume = resume.model_copy(update={"intervention_id": new_id()})

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
