from pathlib import Path

import httpx
import pytest

from mishkan.application import ApplicationCommand
from mishkan.config.loader import ConfigLoader
from mishkan.config.models import MishkanConfig, ProjectConfig
from mishkan.config.presets import preset_text
from mishkan.conversations import (
    ChannelClass,
    ConversationChannel,
    ConversationMessage,
    EscalationOption,
    ExecutiveRecommendation,
    InterventionKind,
    InterventionTargetKind,
    MissionEscalation,
    MissionIntervention,
)
from mishkan.daemon import DaemonBootstrap, create_app
from mishkan.daemon.auth import TokenFile
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
)
from mishkan.organization import load_canonical_organization


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _config(tmp_path: Path) -> MishkanConfig:
    source = tmp_path / "config.yaml"
    source.write_text(preset_text("local"), encoding="utf-8")
    loaded = ConfigLoader().load([source]).value
    return loaded.model_copy(update={"project": ProjectConfig(workspace=tmp_path)})


def _confirmation(identity_id: str) -> ExecutiveConfirmation:
    return ExecutiveConfirmation(
        identity_id=identity_id,
        disposition="confirmed",
        rationale=f"{identity_id} confirms the mission coverage",
        evidence_references=(f"evidence:{identity_id.lower()}",),
        coverage=("product" if identity_id == "PM" else "technical-security-quality",),
    )


def _selection(identity_id: str) -> CrewSelectionEvidence:
    return CrewSelectionEvidence(
        project_references=("repository:api",),
        competence_references=(f"profile:{identity_id}:competence",),
        availability_references=(f"profile:{identity_id}:availability",),
        conflict_assessment="No production, evaluation, reporting, or ownership conflict",
        risk_coverage=("recovery correctness",),
        independence_references=(f"profile:{identity_id}:independence",),
    )


def _brief(mission: MissionRecord) -> MissionBrief:
    return MissionBrief(
        mission_id=mission.mission_id,
        version=1,
        organization_id=mission.organization_id,
        organization_version=mission.organization_version,
        status=MissionBriefStatus.CONFIRMED,
        objective=mission.origin.objective,
        problem="Users cannot recover access after losing a credential",
        desired_outcome="A verified recovery flow with explicit residual risk",
        scope=("repository:api",),
        exclusions=("production deployment",),
        acceptance_criteria=("independent recovery test passes",),
        constraints=("preserve existing accounts",),
        risks=("account takeover",),
        authority_scope=("repository:api",),
        proposed_crew=(
            "Backend_Service_Engineer",
            "Product_Functional_Evaluator",
            "Technical_Change_Reporter",
        ),
        evidence_requirements=("test report", "security review"),
        escalation_conditions=("security tradeoff exceeds accepted risk",),
        environment_intent=MissionEnvironmentIntent(
            required_evidence=("environment readiness",),
            environment_dependent=True,
        ),
        pm_confirmation=_confirmation("PM"),
        cto_confirmation=_confirmation("CTO"),
    )


def _crew(brief: MissionBrief) -> MissionCrewRevision:
    assert brief.pm_confirmation is not None
    assert brief.cto_confirmation is not None
    members = tuple(
        MissionCrewMember(
            identity_id=identity_id,
            assignment_kind=assignment,
            responsibility=responsibility,
            selection_evidence=_selection(identity_id),
        )
        for identity_id, assignment, responsibility in (
            (
                "Backend_Service_Engineer",
                CrewAssignmentKind.PRODUCTION,
                "Implement the accepted recovery scope",
            ),
            (
                "Product_Functional_Evaluator",
                CrewAssignmentKind.EVALUATION,
                "Evaluate recovery behavior independently",
            ),
            (
                "Technical_Change_Reporter",
                CrewAssignmentKind.REPORTING,
                "Report verified outcomes and residual risks",
            ),
        )
    )
    return MissionCrewRevision(
        mission_id=brief.mission_id,
        version=1,
        organization_id=brief.organization_id,
        organization_version=brief.organization_version,
        brief_version=brief.version,
        mission_lead_id="Backend_Service_Engineer",
        members=members,
        pm_composition_confirmation_id=brief.pm_confirmation.confirmation_id,
        cto_coverage_confirmation_id=brief.cto_confirmation.confirmation_id,
        revision_reason="Initial evidence-based composition",
    )


@pytest.mark.anyio
async def test_mission_brief_and_contextual_crew_use_the_common_daemon_authority(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read()
    headers = {"Authorization": f"Bearer {token.token}"}
    organization = load_canonical_organization()
    mission = MissionRecord(
        origin=MissionOrigin(
            kind=MissionOriginKind.CEO,
            actor_id="ceo:y4nn777",
            objective="Add durable account recovery",
        ),
        organization_id=organization.organization_id,
        organization_version=organization.organization_version,
    )
    brief = _brief(mission)
    crew = _crew(brief)

    commands = (
        ApplicationCommand(
            command_type="mission.create",
            actor_id=token.principal_id,
            target_type="mission",
            target_id=str(mission.mission_id),
            expected_revision=0,
            payload={"record": mission.model_dump(mode="json")},
        ),
        ApplicationCommand(
            command_type="mission.brief.record",
            actor_id=token.principal_id,
            target_type="mission",
            target_id=str(mission.mission_id),
            expected_revision=1,
            payload={"brief": brief.model_dump(mode="json")},
        ),
        ApplicationCommand(
            command_type="mission.crew.record",
            actor_id=token.principal_id,
            target_type="mission",
            target_id=str(mission.mission_id),
            expected_revision=2,
            payload={"crew": crew.model_dump(mode="json")},
        ),
    )

    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        results = []
        for command in commands:
            response = await client.post(
                "/v1/commands", headers=headers, json=command.model_dump(mode="json")
            )
            assert response.status_code == 200
            assert response.json()["status"] == "accepted"
            results.append(response.json())

        replay = await client.post(
            "/v1/commands", headers=headers, json=commands[-1].model_dump(mode="json")
        )
        roster_response = await client.get("/v1/organization", headers=headers)
        mission_response = await client.get(f"/v1/missions/{mission.mission_id}", headers=headers)
        brief_response = await client.get(
            f"/v1/missions/{mission.mission_id}/brief", headers=headers
        )
        crew_response = await client.get(f"/v1/missions/{mission.mission_id}/crew", headers=headers)

    assert replay.json() == results[-1]
    assert len(roster_response.json()["identities"]) == 59
    durable = MissionRecord.model_validate(mission_response.json())
    assert durable.revision == 3
    assert MissionBrief.model_validate(brief_response.json()) == brief
    assert MissionCrewRevision.model_validate(crew_response.json()) == crew


@pytest.mark.anyio
async def test_mission_command_refuses_a_stale_application_revision(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read()
    organization = load_canonical_organization()
    mission = MissionRecord(
        origin=MissionOrigin(
            kind=MissionOriginKind.CEO,
            actor_id="ceo:y4nn777",
            objective="Verify stale mission commands",
        ),
        organization_id=organization.organization_id,
        organization_version=organization.organization_version,
    )
    create = ApplicationCommand(
        command_type="mission.create",
        actor_id=token.principal_id,
        target_type="mission",
        target_id=str(mission.mission_id),
        expected_revision=0,
        payload={"record": mission.model_dump(mode="json")},
    )
    stale = ApplicationCommand(
        command_type="mission.brief.record",
        actor_id=token.principal_id,
        target_type="mission",
        target_id=str(mission.mission_id),
        expected_revision=0,
        payload={"brief": _brief(mission).model_dump(mode="json")},
    )
    headers = {"Authorization": f"Bearer {token.token}"}
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/v1/commands", headers=headers, json=create.model_dump(mode="json"))
        response = await client.post(
            "/v1/commands", headers=headers, json=stale.model_dump(mode="json")
        )

    assert response.status_code == 409
    assert response.json()["code"] == "ERR-REV-001"


@pytest.mark.anyio
async def test_conversation_escalation_and_intervention_share_daemon_semantics(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read()
    organization = load_canonical_organization()
    mission = MissionRecord(
        origin=MissionOrigin(
            kind=MissionOriginKind.CEO,
            actor_id="CEO",
            objective="Resolve a bounded PM and CTO disagreement",
        ),
        organization_id=organization.organization_id,
        organization_version=organization.organization_version,
    )
    brief = _brief(mission)
    channel = ConversationChannel(
        channel_class=ChannelClass.MISSION,
        title="Bounded disagreement",
        participants=("CEO", "PM", "CTO"),
        mission_id=mission.mission_id,
        created_by="PM",
    )
    message = ConversationMessage(
        conversation_id=channel.conversation_id,
        author_identity="CTO",
        body="This recommendation is evidence, not an implicit command.",
    )
    escalation = MissionEscalation(
        mission_id=mission.mission_id,
        conversation_id=channel.conversation_id,
        raised_by="PM",
        blocked_scope=("task:dependent",),
        decision_required="Choose whether the bounded risk is acceptable",
        reason="PM and CTO recommendations differ",
        options=(
            EscalationOption(
                option_id="accept",
                description="Accept the bounded risk",
                consequences=("dependent work continues",),
                risks=("compatibility remains imperfect",),
            ),
            EscalationOption(
                option_id="reject",
                description="Require remediation first",
                consequences=("dependent work remains paused",),
                risks=("delivery is delayed",),
            ),
        ),
        recommendations=(
            ExecutiveRecommendation(
                identity_id="PM",
                recommended_option_id="accept",
                rationale="User benefit outweighs the bounded risk",
                evidence_references=("evidence:product",),
            ),
            ExecutiveRecommendation(
                identity_id="CTO",
                recommended_option_id="reject",
                rationale="Technical evidence remains incomplete",
                evidence_references=("evidence:technical",),
            ),
        ),
        uncertainty=("legacy compatibility",),
        independent_work_continuing=("task:independent",),
        evidence_references=("evidence:joint-review",),
    )
    intervention = MissionIntervention(
        mission_id=mission.mission_id,
        conversation_id=channel.conversation_id,
        actor_id="CEO",
        kind=InterventionKind.ANSWER_ESCALATION,
        target_kind=InterventionTargetKind.ESCALATION,
        target_id=str(escalation.escalation_id),
        reason="The product evidence supports accepting the bounded risk",
        scope=("task:dependent",),
        confirmation="Accept the risk for the dependent task only",
        authority_reference="authority:ceo",
        evidence_references=("evidence:ceo-answer",),
        escalation_id=escalation.escalation_id,
        effect="Answer the escalation without pausing independent work",
    )
    commands = (
        ApplicationCommand(
            command_type="mission.create",
            actor_id=token.principal_id,
            target_type="mission",
            target_id=str(mission.mission_id),
            expected_revision=0,
            payload={"record": mission.model_dump(mode="json")},
        ),
        ApplicationCommand(
            command_type="mission.brief.record",
            actor_id=token.principal_id,
            target_type="mission",
            target_id=str(mission.mission_id),
            expected_revision=1,
            payload={"brief": brief.model_dump(mode="json")},
        ),
        ApplicationCommand(
            command_type="conversation.create",
            actor_id=token.principal_id,
            target_type="conversation",
            target_id=str(channel.conversation_id),
            expected_revision=0,
            payload={"channel": channel.model_dump(mode="json")},
        ),
        ApplicationCommand(
            command_type="conversation.message.post",
            actor_id=token.principal_id,
            target_type="conversation_message",
            target_id=str(message.message_id),
            expected_revision=0,
            payload={"message": message.model_dump(mode="json")},
        ),
        ApplicationCommand(
            command_type="mission.escalation.open",
            actor_id=token.principal_id,
            target_type="mission_escalation",
            target_id=str(escalation.escalation_id),
            expected_revision=0,
            payload={"escalation": escalation.model_dump(mode="json")},
        ),
        ApplicationCommand(
            command_type="mission.intervention.apply",
            actor_id=token.principal_id,
            target_type="mission",
            target_id=str(mission.mission_id),
            expected_revision=2,
            payload={"intervention": intervention.model_dump(mode="json")},
        ),
    )
    headers = {"Authorization": f"Bearer {token.token}"}
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for command in commands:
            response = await client.post(
                "/v1/commands", headers=headers, json=command.model_dump(mode="json")
            )
            assert response.status_code == 200
            assert response.json()["status"] == "accepted"
        messages = await client.get(
            f"/v1/conversations/{channel.conversation_id}/messages", headers=headers
        )
        escalations = await client.get(
            f"/v1/missions/{mission.mission_id}/escalations", headers=headers
        )
        interventions = await client.get(
            f"/v1/missions/{mission.mission_id}/interventions", headers=headers
        )
        durable_mission = await client.get(f"/v1/missions/{mission.mission_id}", headers=headers)

    assert [item["message_id"] for item in messages.json()] == [str(message.message_id)]
    assert escalations.json()[0]["state"] == "answered"
    assert interventions.json()[0]["intervention_id"] == str(intervention.intervention_id)
    assert durable_mission.json()["revision"] == 3
