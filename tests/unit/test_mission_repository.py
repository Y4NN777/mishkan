from pathlib import Path

import pytest

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
    MissionTransition,
    SQLiteMissionRepository,
)
from mishkan.organization import load_canonical_organization
from mishkan.persistence import SQLiteApplicationRepository
from mishkan.persistence.migration import SchemaManager


def _confirmation(identity_id: str) -> ExecutiveConfirmation:
    return ExecutiveConfirmation(
        identity_id=identity_id,
        disposition="confirmed",
        rationale=f"{identity_id} confirms the accountable mission coverage",
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


def _setup(tmp_path: Path) -> tuple[Path, SQLiteMissionRepository, MissionRecord]:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    repository = SQLiteMissionRepository(database)
    organization = load_canonical_organization()
    repository.record_organization(organization)
    mission = repository.create_mission(
        MissionRecord(
            origin=MissionOrigin(
                kind=MissionOriginKind.CEO,
                actor_id="ceo:y4nn777",
                objective="Add durable account recovery",
            ),
            organization_id=organization.organization_id,
            organization_version=organization.organization_version,
        )
    )
    return database, repository, mission


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
            environment_dependent=True,
        ),
        pm_confirmation=_confirmation("PM"),
        cto_confirmation=_confirmation("CTO"),
    )


def _crew(brief: MissionBrief) -> MissionCrewRevision:
    assert brief.pm_confirmation is not None
    assert brief.cto_confirmation is not None
    return MissionCrewRevision(
        mission_id=brief.mission_id,
        version=1,
        organization_id=brief.organization_id,
        organization_version=brief.organization_version,
        brief_version=brief.version,
        mission_lead_id="Backend_Service_Engineer",
        members=(
            MissionCrewMember(
                identity_id="Backend_Service_Engineer",
                assignment_kind=CrewAssignmentKind.PRODUCTION,
                responsibility="Implement only the accepted recovery scope",
                selection_evidence=_selection("Backend_Service_Engineer"),
            ),
            MissionCrewMember(
                identity_id="Product_Functional_Evaluator",
                assignment_kind=CrewAssignmentKind.EVALUATION,
                responsibility="Evaluate recovery behavior independently",
                selection_evidence=_selection("Product_Functional_Evaluator"),
            ),
            MissionCrewMember(
                identity_id="Technical_Change_Reporter",
                assignment_kind=CrewAssignmentKind.REPORTING,
                responsibility="Report verified results and residual risks",
                selection_evidence=_selection("Technical_Change_Reporter"),
            ),
        ),
        pm_composition_confirmation_id=brief.pm_confirmation.confirmation_id,
        cto_coverage_confirmation_id=brief.cto_confirmation.confirmation_id,
        revision_reason="Initial evidence-based composition",
    )


def test_mission_brief_and_crew_revisions_survive_repository_reopen(tmp_path: Path) -> None:
    database, repository, mission = _setup(tmp_path)
    brief = _brief(mission)
    repository.record_brief(brief, expected_revision=mission.revision)
    after_brief = repository.mission(str(mission.mission_id))
    crew = _crew(brief)
    repository.record_crew(crew, expected_revision=after_brief.revision)

    reopened = SQLiteMissionRepository(database)
    durable = reopened.mission(str(mission.mission_id))

    assert durable.state is MissionState.CLARIFYING
    assert durable.revision == 3
    assert durable.current_brief_version == 1
    assert durable.current_crew_version == 1
    assert reopened.brief(str(mission.mission_id)) == brief
    assert reopened.crew(str(mission.mission_id)) == crew
    assert reopened.list_missions() == (durable,)
    events = SQLiteApplicationRepository(database).events(entity_id=str(mission.mission_id))
    assert [event.event_type for event in events.events] == [
        "mission.proposed",
        "mission.brief_recorded",
        "mission.crew_recorded",
    ]


def test_mission_mutations_require_current_revision_and_are_idempotent(tmp_path: Path) -> None:
    _, repository, mission = _setup(tmp_path)
    brief = _brief(mission)

    assert repository.record_brief(brief, expected_revision=mission.revision) == brief
    assert repository.record_brief(brief, expected_revision=mission.revision) == brief

    changed = brief.model_copy(update={"problem": "Different immutable content"})
    with pytest.raises(MishkanError) as duplicate:
        repository.record_brief(changed, expected_revision=mission.revision)
    assert duplicate.value.envelope.code is ErrorCode.DUPLICATE_RESULT

    second = brief.model_copy(update={"brief_id": new_id(), "version": 2})
    with pytest.raises(MishkanError) as stale:
        repository.record_brief(second, expected_revision=mission.revision)
    assert stale.value.envelope.code is ErrorCode.REVISION_MISMATCH


def test_crew_assignment_uses_profile_authority_and_confirmed_composition(tmp_path: Path) -> None:
    _, repository, mission = _setup(tmp_path)
    original = _brief(mission)
    invalid_brief = original.model_copy(
        update={
            "proposed_crew": (
                "Product_Analyst",
                "Backend_Service_Engineer",
                "Technical_Change_Reporter",
            )
        }
    )
    repository.record_brief(invalid_brief, expected_revision=mission.revision)
    current = repository.mission(str(mission.mission_id))
    assert invalid_brief.pm_confirmation is not None
    assert invalid_brief.cto_confirmation is not None
    invalid_crew = MissionCrewRevision(
        mission_id=mission.mission_id,
        version=1,
        organization_id=mission.organization_id,
        organization_version=mission.organization_version,
        brief_version=1,
        mission_lead_id="Backend_Service_Engineer",
        members=(
            MissionCrewMember(
                identity_id="Backend_Service_Engineer",
                assignment_kind=CrewAssignmentKind.PRODUCTION,
                responsibility="Produce the change",
                selection_evidence=_selection("Backend_Service_Engineer"),
            ),
            MissionCrewMember(
                identity_id="Product_Analyst",
                assignment_kind=CrewAssignmentKind.EVALUATION,
                responsibility="Attempt an unauthorized independent evaluation",
                selection_evidence=_selection("Product_Analyst"),
            ),
            MissionCrewMember(
                identity_id="Technical_Change_Reporter",
                assignment_kind=CrewAssignmentKind.REPORTING,
                responsibility="Report the result",
                selection_evidence=_selection("Technical_Change_Reporter"),
            ),
        ),
        pm_composition_confirmation_id=invalid_brief.pm_confirmation.confirmation_id,
        cto_coverage_confirmation_id=invalid_brief.cto_confirmation.confirmation_id,
        revision_reason="Invalid profile assignment fixture",
    )

    with pytest.raises(MishkanError) as conflict:
        repository.record_crew(invalid_crew, expected_revision=current.revision)
    assert conflict.value.envelope.code is ErrorCode.ROLE_CONFLICT


def test_accountable_assignment_and_lifecycle_are_explicit_and_durable(tmp_path: Path) -> None:
    _, repository, mission = _setup(tmp_path)
    brief = _brief(mission)
    repository.record_brief(brief, expected_revision=mission.revision)
    after_brief = repository.mission(str(mission.mission_id))
    crew = _crew(brief)
    repository.record_crew(crew, expected_revision=after_brief.revision)
    ready = repository.mission(str(mission.mission_id))
    assignment = MissionTaskAssignment(
        mission_id=mission.mission_id,
        crew_version=crew.version,
        task_id="implement-recovery",
        accountable_owner="Backend_Service_Engineer",
        assignment_kind=CrewAssignmentKind.PRODUCTION,
        contributors=(),
        expected_result="A verified recovery implementation",
        completion_criteria=("independent recovery test passes",),
        dependencies=(),
        authority_scope=("repository:api",),
        exact_tools=("file.read", "file.patch", "process.run"),
        path_scopes=("repository:api",),
        limits=(MissionResourceLimit(name="wall_time", value=1800, unit="seconds"),),
        required_evidence=("test report", "change set"),
    )
    repository.record_assignment(assignment)
    planned = MissionTransition(
        mission_id=mission.mission_id,
        from_state=MissionState.CLARIFYING,
        to_state=MissionState.PLANNED,
        actor_or_cause="PM+CTO",
        reason="Jointly confirmed Brief, crew, and assignment are ready",
        affected_scope=("mission:all",),
        evidence_references=(f"brief:{brief.brief_id}", f"crew:{crew.crew_id}"),
    )
    repository.transition(planned, expected_revision=ready.revision)
    after_planned = repository.mission(str(mission.mission_id))
    active = MissionTransition(
        mission_id=mission.mission_id,
        from_state=MissionState.PLANNED,
        to_state=MissionState.ACTIVE,
        actor_or_cause="Mission_Lead responsibility",
        reason="Accountable task assignment is eligible",
        affected_scope=("task:implement-recovery",),
        evidence_references=(f"assignment:{assignment.assignment_id}",),
    )
    repository.transition(active, expected_revision=after_planned.revision)

    assert repository.assignments(str(mission.mission_id)) == (assignment,)
    assert repository.transitions(str(mission.mission_id)) == (planned, active)
    assert repository.mission(str(mission.mission_id)).state is MissionState.ACTIVE


def test_assignment_rejects_identity_outside_current_contextual_crew(tmp_path: Path) -> None:
    _, repository, mission = _setup(tmp_path)
    brief = _brief(mission)
    repository.record_brief(brief, expected_revision=mission.revision)
    after_brief = repository.mission(str(mission.mission_id))
    repository.record_crew(_crew(brief), expected_revision=after_brief.revision)
    assignment = MissionTaskAssignment(
        mission_id=mission.mission_id,
        crew_version=1,
        task_id="unscoped-work",
        accountable_owner="Android_Engineer",
        assignment_kind=CrewAssignmentKind.PRODUCTION,
        expected_result="An unauthorized result",
        completion_criteria=("result exists",),
        authority_scope=("repository:api",),
        exact_tools=(),
        path_scopes=(),
        limits=(MissionResourceLimit(name="wall_time", value=60, unit="seconds"),),
        required_evidence=("result",),
    )

    with pytest.raises(MishkanError) as error:
        repository.record_assignment(assignment)
    assert error.value.envelope.code is ErrorCode.MISSION


def test_active_transition_refuses_crew_and_assignments_from_an_older_brief(
    tmp_path: Path,
) -> None:
    _, repository, mission = _setup(tmp_path)
    brief = _brief(mission)
    repository.record_brief(brief, expected_revision=mission.revision)
    current = repository.mission(str(mission.mission_id))
    crew = _crew(brief)
    repository.record_crew(crew, expected_revision=current.revision)
    assignment = MissionTaskAssignment(
        mission_id=mission.mission_id,
        crew_version=crew.version,
        task_id="implement-recovery",
        accountable_owner="Backend_Service_Engineer",
        assignment_kind=CrewAssignmentKind.PRODUCTION,
        expected_result="A verified recovery implementation",
        completion_criteria=("result is reviewable",),
        authority_scope=("repository:api",),
        exact_tools=("file.read",),
        path_scopes=("repository:api",),
        limits=(MissionResourceLimit(name="wall_time", value=600, unit="seconds"),),
        required_evidence=("artifact:result",),
    )
    repository.record_assignment(assignment)
    current = repository.mission(str(mission.mission_id))
    repository.transition(
        MissionTransition(
            mission_id=mission.mission_id,
            from_state=MissionState.CLARIFYING,
            to_state=MissionState.PLANNED,
            actor_or_cause="PM+CTO",
            reason="The initial mission plan is ready",
            affected_scope=("mission:all",),
            evidence_references=(f"assignment:{assignment.assignment_id}",),
        ),
        expected_revision=current.revision,
    )
    current = repository.mission(str(mission.mission_id))
    revised_brief = brief.model_copy(
        update={
            "brief_id": new_id(),
            "version": 2,
            "problem": "New evidence changes the governed mission context",
        }
    )
    repository.record_brief(revised_brief, expected_revision=current.revision)
    current = repository.mission(str(mission.mission_id))
    repository.transition(
        MissionTransition(
            mission_id=mission.mission_id,
            from_state=MissionState.CLARIFYING,
            to_state=MissionState.PLANNED,
            actor_or_cause="PM+CTO",
            reason="The revised Brief requires a matching crew revision",
            affected_scope=("mission:all",),
            evidence_references=(f"brief:{revised_brief.brief_id}",),
        ),
        expected_revision=current.revision,
    )
    current = repository.mission(str(mission.mission_id))

    with pytest.raises(MishkanError) as stale:
        repository.transition(
            MissionTransition(
                mission_id=mission.mission_id,
                from_state=MissionState.PLANNED,
                to_state=MissionState.ACTIVE,
                actor_or_cause="Mission_Lead",
                reason="Attempt to reuse obsolete mission lineage",
                affected_scope=("mission:all",),
                evidence_references=(f"crew:{crew.crew_id}",),
            ),
            expected_revision=current.revision,
        )

    assert stale.value.envelope.code is ErrorCode.REVISION_MISMATCH


@pytest.mark.parametrize(
    ("owner", "kind", "contributors"),
    (
        (
            "Product_Functional_Evaluator",
            CrewAssignmentKind.PRODUCTION,
            (),
        ),
        (
            "Backend_Service_Engineer",
            CrewAssignmentKind.PRODUCTION,
            ("Product_Functional_Evaluator",),
        ),
    ),
)
def test_assignment_rejects_cross_responsibility_production_and_evaluation(
    tmp_path: Path,
    owner: str,
    kind: CrewAssignmentKind,
    contributors: tuple[str, ...],
) -> None:
    _, repository, mission = _setup(tmp_path)
    brief = _brief(mission)
    repository.record_brief(brief, expected_revision=mission.revision)
    current = repository.mission(str(mission.mission_id))
    repository.record_crew(_crew(brief), expected_revision=current.revision)
    assignment = MissionTaskAssignment(
        mission_id=mission.mission_id,
        crew_version=1,
        task_id="conflicted-production",
        accountable_owner=owner,
        assignment_kind=kind,
        contributors=contributors,
        expected_result="A production result with invalid responsibility separation",
        completion_criteria=("result exists",),
        authority_scope=("repository:api",),
        exact_tools=("file.read",),
        path_scopes=("repository:api",),
        limits=(MissionResourceLimit(name="wall_time", value=60, unit="seconds"),),
        required_evidence=("result",),
    )

    with pytest.raises(MishkanError) as error:
        repository.record_assignment(assignment)

    assert error.value.envelope.code is ErrorCode.ROLE_CONFLICT
