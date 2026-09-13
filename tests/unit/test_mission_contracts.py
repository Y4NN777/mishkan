from uuid import UUID

import pytest
from pydantic import ValidationError

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
    MissionState,
)


def _confirmation(identity_id: str) -> ExecutiveConfirmation:
    return ExecutiveConfirmation(
        identity_id=identity_id,
        disposition="confirmed",
        rationale=f"{identity_id} confirms its accountable coverage",
        evidence_references=(f"evidence:{identity_id.lower()}",),
        coverage=(
            ("product", "composition")
            if identity_id == "PM"
            else ("technical", "security", "quality")
        ),
    )


def _intent(*, environment_dependent: bool = True) -> MissionEnvironmentIntent:
    return MissionEnvironmentIntent(
        known_locations=("repository:api",),
        target_platforms=("linux",),
        target_architectures=("x86_64",),
        existing_definition_references=("repo:.devcontainer/devcontainer.json",),
        isolation_requirements=("project isolation",),
        network_requirements=("dependency registry",),
        credential_references=("credential:registry",),
        resource_constraints=("memory:2GiB",),
        required_evidence=("environment readiness",),
        known_unknowns=(),
        environment_dependent=environment_dependent,
    )


def _brief(mission_id: UUID, **updates: object) -> MissionBrief:
    pm = _confirmation("PM")
    cto = _confirmation("CTO")
    data: dict[str, object] = {
        "mission_id": mission_id,
        "version": 1,
        "organization_id": "mishkan-organization",
        "organization_version": "1",
        "status": MissionBriefStatus.CONFIRMED,
        "objective": "Add durable account recovery",
        "problem": "Users cannot recover access after losing a credential",
        "desired_outcome": "A verified recovery flow with documented residual risk",
        "scope": ("api", "client"),
        "exclusions": ("production deployment",),
        "acceptance_criteria": ("independent recovery test passes",),
        "constraints": ("preserve current accounts",),
        "risks": ("account takeover",),
        "authority_scope": ("repository:api", "repository:client"),
        "proposed_crew": (
            "Backend_Service_Engineer",
            "Product_Functional_Evaluator",
            "Technical_Change_Reporter",
        ),
        "evidence_requirements": ("test report",),
        "escalation_conditions": ("security tradeoff exceeds accepted risk",),
        "environment_intent": _intent(),
        "pm_confirmation": pm,
        "cto_confirmation": cto,
    }
    data.update(updates)
    return MissionBrief.model_validate(data)


def _selection(label: str) -> CrewSelectionEvidence:
    return CrewSelectionEvidence(
        project_references=(f"project:{label}",),
        competence_references=(f"competence:{label}",),
        availability_references=(f"availability:{label}",),
        conflict_assessment="No unresolved conflict was found",
        risk_coverage=("mission risk",),
        independence_references=(f"independence:{label}",),
    )


def test_confirmed_brief_requires_joint_pm_cto_evidence() -> None:
    mission_id = MissionRecord(
        origin=MissionOrigin(
            kind=MissionOriginKind.CEO,
            actor_id="ceo:y4nn777",
            objective="Add durable account recovery",
        ),
        organization_id="mishkan-organization",
        organization_version="1",
    ).mission_id

    brief = _brief(mission_id)

    assert brief.pm_confirmation is not None
    assert brief.cto_confirmation is not None
    assert brief.fingerprint == brief.fingerprint

    with pytest.raises(ValidationError, match="requires PM and CTO confirmation"):
        _brief(mission_id, cto_confirmation=None)

    incomplete_pm = _confirmation("PM").model_copy(update={"coverage": ("product",)})
    with pytest.raises(ValidationError, match="PM composition coverage"):
        _brief(mission_id, pm_confirmation=incomplete_pm)

    incomplete_cto = _confirmation("CTO").model_copy(update={"coverage": ("technical", "quality")})
    with pytest.raises(ValidationError, match="CTO technical, security, and quality"):
        _brief(mission_id, cto_confirmation=incomplete_cto)


def test_environment_dependency_requires_evidence_or_explicit_unknown() -> None:
    with pytest.raises(ValidationError, match="evidence requirements or explicit unknowns"):
        MissionEnvironmentIntent(environment_dependent=True)


def test_mission_lead_is_a_temporary_member_and_cannot_be_reporter() -> None:
    mission = MissionRecord(
        origin=MissionOrigin(
            kind=MissionOriginKind.INCIDENT,
            actor_id="incident:auth-outage",
            objective="Restore authentication safely",
        ),
        organization_id="mishkan-organization",
        organization_version="1",
    )
    brief = _brief(mission.mission_id)
    producer = MissionCrewMember(
        identity_id="Backend_Service_Engineer",
        assignment_kind=CrewAssignmentKind.PRODUCTION,
        responsibility="Implement the accepted recovery change",
        selection_evidence=_selection("producer"),
    )
    evaluator = MissionCrewMember(
        identity_id="Product_Functional_Evaluator",
        assignment_kind=CrewAssignmentKind.EVALUATION,
        responsibility="Evaluate the recovery behavior independently",
        selection_evidence=_selection("evaluator"),
    )
    reporter = MissionCrewMember(
        identity_id="Technical_Change_Reporter",
        assignment_kind=CrewAssignmentKind.REPORTING,
        responsibility="Report verified outcomes and limitations",
        selection_evidence=_selection("reporter"),
    )

    crew = MissionCrewRevision(
        mission_id=mission.mission_id,
        version=1,
        organization_id=mission.organization_id,
        organization_version=mission.organization_version,
        brief_version=brief.version,
        mission_lead_id=producer.identity_id,
        members=(producer, evaluator, reporter),
        pm_composition_confirmation_id=brief.pm_confirmation.confirmation_id,
        cto_coverage_confirmation_id=brief.cto_confirmation.confirmation_id,
        revision_reason="Initial contextual composition",
    )

    assert crew.mission_lead_id == "Backend_Service_Engineer"
    assert crew.fingerprint == crew.fingerprint

    invalid = crew.model_dump(mode="python")
    invalid["mission_lead_id"] = reporter.identity_id
    with pytest.raises(ValidationError, match="Mission Lead cannot report"):
        MissionCrewRevision.model_validate(invalid)


def test_advanced_mission_state_requires_a_durable_brief() -> None:
    with pytest.raises(ValidationError, match="require a durable Mission Brief"):
        MissionRecord(
            state=MissionState.ACTIVE,
            origin=MissionOrigin(
                kind=MissionOriginKind.PM,
                actor_id="PM",
                objective="Measure onboarding friction",
            ),
            organization_id="mishkan-organization",
            organization_version="1",
        )
