import subprocess
from pathlib import Path
from typing import Literal

import pytest
from support.capabilities import resolved_tool_lineage

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.identity import new_id
from mishkan.missions import (
    AssignmentChangeKind,
    CrewAssignmentKind,
    CrewSelectionEvidence,
    ExecutiveConfirmation,
    MissionAssignmentChange,
    MissionBrief,
    MissionBriefStatus,
    MissionCrewMember,
    MissionCrewRevision,
    MissionEnvironmentIntent,
    MissionOrigin,
    MissionOriginKind,
    MissionRecord,
    MissionResourceLimit,
    MissionRunAcceptance,
    MissionRunBinding,
    MissionState,
    MissionTaskAssignment,
    MissionTransition,
    SQLiteMissionRepository,
)
from mishkan.organization import load_canonical_organization
from mishkan.persistence import LocalRunRepository, SQLiteApplicationRepository
from mishkan.persistence.migration import SchemaManager
from mishkan.planning import (
    AcceptedPlan,
    PlanExecutionContext,
    PlanOrganizationBinding,
    PlanTask,
)
from mishkan.planning.models import InitializationResult, ReviewDecision
from mishkan.repository import RepositoryInspector


def _confirmation(identity_id: Literal["PM", "CTO"]) -> ExecutiveConfirmation:
    return ExecutiveConfirmation(
        identity_id=identity_id,
        disposition="confirmed",
        rationale=f"{identity_id} confirms the accountable mission coverage",
        evidence_references=(f"evidence:{identity_id.lower()}",),
        coverage=(
            ("product", "composition")
            if identity_id == "PM"
            else ("technical", "security", "quality")
        ),
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
                schema_version="1.1",
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


def _assignment_change_fixture(
    tmp_path: Path,
) -> tuple[
    SQLiteMissionRepository,
    MissionRecord,
    MissionCrewRevision,
    MissionTaskAssignment,
]:
    _, repository, mission = _setup(tmp_path)
    brief = _brief(mission).model_copy(
        update={
            "proposed_crew": (
                *_brief(mission).proposed_crew,
                "Android_Engineer",
            )
        }
    )
    repository.record_brief(brief, expected_revision=mission.revision)
    current = repository.mission(str(mission.mission_id))
    original_crew = _crew(brief)
    crew = original_crew.model_copy(
        update={
            "members": (
                *original_crew.members,
                MissionCrewMember(
                    identity_id="Android_Engineer",
                    assignment_kind=CrewAssignmentKind.PRODUCTION,
                    responsibility="Contribute production work within the accepted task contract",
                    selection_evidence=_selection("Android_Engineer"),
                ),
            )
        }
    )
    repository.record_crew(crew, expected_revision=current.revision)
    assignment = MissionTaskAssignment(
        mission_id=mission.mission_id,
        crew_version=crew.version,
        task_id="implement-recovery",
        accountable_owner="Backend_Service_Engineer",
        assignment_kind=CrewAssignmentKind.PRODUCTION,
        expected_result="A verified recovery implementation",
        completion_criteria=("independent recovery test passes",),
        authority_scope=("repository:api",),
        exact_tools=("file.read", "file.patch"),
        path_scopes=("repository:api",),
        limits=(MissionResourceLimit(name="wall_time", value=600, unit="seconds"),),
        required_evidence=("artifact:result", "artifact:evaluation"),
        requires_independent_evaluation=True,
    )
    repository.record_assignment(assignment)
    return repository, mission, crew, assignment


def _revise_assignment(
    prior: MissionTaskAssignment,
    change: MissionAssignmentChange,
    **updates: object,
) -> MissionTaskAssignment:
    payload = prior.model_dump(mode="json")
    payload.update(
        {
            "schema_version": "1.1",
            "assignment_id": str(new_id()),
            "assignment_revision": prior.assignment_revision + 1,
            "change": change.model_dump(mode="json"),
            **updates,
        }
    )
    return MissionTaskAssignment.model_validate(payload)


def test_assignment_changes_distinguish_local_formal_and_replanned_authority(
    tmp_path: Path,
) -> None:
    repository, mission, crew, initial = _assignment_change_fixture(tmp_path)
    plan_a = "a" * 64
    local = _revise_assignment(
        initial,
        MissionAssignmentChange(
            prior_assignment_id=initial.assignment_id,
            prior_assignment_revision=initial.assignment_revision,
            requested_by_identity=crew.mission_lead_id,
            change_kind=AssignmentChangeKind.IN_PLAN_LOCAL,
            rationale="Add one available contributor without changing the task contract",
            context_references=("mission:current-crew",),
            evidence_references=("evidence:android-availability",),
            authority_reference="authority:mission-lead-local-assignment",
            prior_plan_fingerprint=plan_a,
            effective_plan_fingerprint=plan_a,
        ),
        contributors=("Android_Engineer",),
    )
    assert repository.record_assignment(local) == local

    cto = _confirmation("CTO")
    pm = _confirmation("PM")
    formal = _revise_assignment(
        local,
        MissionAssignmentChange(
            prior_assignment_id=local.assignment_id,
            prior_assignment_revision=local.assignment_revision,
            requested_by_identity=crew.mission_lead_id,
            change_kind=AssignmentChangeKind.FORMAL_REASSIGNMENT,
            rationale="Transfer accountability while preserving the accepted task contract",
            context_references=("mission:current-crew", "task:implement-recovery"),
            evidence_references=("evidence:coverage-review",),
            authority_reference="authority:pm-formal-reassignment",
            prior_plan_fingerprint=plan_a,
            effective_plan_fingerprint=plan_a,
            cto_coverage_confirmation=cto,
            pm_reassignment_confirmation=pm,
        ),
        accountable_owner="Android_Engineer",
        contributors=("Backend_Service_Engineer",),
    )
    assert repository.record_assignment(formal) == formal

    replanned = _revise_assignment(
        formal,
        MissionAssignmentChange(
            prior_assignment_id=formal.assignment_id,
            prior_assignment_revision=formal.assignment_revision,
            requested_by_identity=crew.mission_lead_id,
            change_kind=AssignmentChangeKind.REPLANNED,
            rationale="A changed bounded result was accepted by a new plan",
            context_references=("mission:current-crew", "plan:new"),
            evidence_references=("evidence:changed-requirement",),
            authority_reference="authority:accepted-replan",
            prior_plan_fingerprint=plan_a,
            effective_plan_fingerprint="b" * 64,
            replanning_evidence_references=("plan:b",),
        ),
        expected_result="A verified recovery implementation with device binding",
    )
    assert repository.record_assignment(replanned) == replanned
    assert repository.assignments(str(mission.mission_id)) == (
        initial,
        local,
        formal,
        replanned,
    )


def test_assignment_change_refuses_non_lead_and_unplanned_contract_change(
    tmp_path: Path,
) -> None:
    repository, _mission, _crew_record, initial = _assignment_change_fixture(tmp_path)
    plan = "a" * 64
    non_lead = _revise_assignment(
        initial,
        MissionAssignmentChange(
            prior_assignment_id=initial.assignment_id,
            prior_assignment_revision=initial.assignment_revision,
            requested_by_identity="PM",
            change_kind=AssignmentChangeKind.IN_PLAN_LOCAL,
            rationale="An identity other than the Mission Lead requested this change",
            context_references=("mission:current-crew",),
            evidence_references=("evidence:request",),
            authority_reference="authority:mission-lead-local-assignment",
            prior_plan_fingerprint=plan,
            effective_plan_fingerprint=plan,
        ),
        contributors=("Android_Engineer",),
    )
    with pytest.raises(MishkanError) as authority:
        repository.record_assignment(non_lead)
    assert authority.value.envelope.code is ErrorCode.AUTHORITY_NOT_GRANTED

    unplanned = _revise_assignment(
        initial,
        MissionAssignmentChange(
            prior_assignment_id=initial.assignment_id,
            prior_assignment_revision=initial.assignment_revision,
            requested_by_identity="Backend_Service_Engineer",
            change_kind=AssignmentChangeKind.IN_PLAN_LOCAL,
            rationale="Attempt to change the result without replanning",
            context_references=("mission:current-crew",),
            evidence_references=("evidence:request",),
            authority_reference="authority:mission-lead-local-assignment",
            prior_plan_fingerprint=plan,
            effective_plan_fingerprint=plan,
        ),
        expected_result="A materially different unplanned result",
    )
    with pytest.raises(MishkanError) as replan:
        repository.record_assignment(unplanned)
    assert replan.value.envelope.code is ErrorCode.PLAN


def _repository_discovery(root: Path, name: str):  # type: ignore[no-untyped-def]
    repository = root / name
    repository.mkdir()
    (repository / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Fixture"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.invalid"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return RepositoryInspector().inspect(repository)


def _run_binding_fixture(
    tmp_path: Path,
    *,
    tool_id: str,
    requires_independent_evaluation: bool = False,
    omit_registry: bool = False,
    corrupt_tool_version: bool = False,
) -> tuple[SQLiteMissionRepository, MissionRunBinding]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database, missions, mission = _setup(tmp_path)
    brief = _brief(mission)
    missions.record_brief(brief, expected_revision=mission.revision)
    current = missions.mission(str(mission.mission_id))
    crew = _crew(brief)
    missions.record_crew(crew, expected_revision=current.revision)
    discovery = _repository_discovery(tmp_path, "binding-repository")
    runs = LocalRunRepository(database)
    run = runs.start_or_resume(discovery, "Execute exact mission work", "mission-binding")
    task = PlanTask(
        task_id="execute-work",
        title="Execute exact work",
        purpose="Exercise the exact accepted mission authority.",
        assigned_role="Backend_Service_Engineer",
        tools=(tool_id,),
        evidence_paths=("README.md",),
    )
    registry, tool_bindings = resolved_tool_lineage(discovery.binding.root, (task,))
    if corrupt_tool_version:
        tool_bindings = (tool_bindings[0].model_copy(update={"tool_version": "99.0.0"}),)
    plan = AcceptedPlan(
        objective="Execute exact mission work",
        outcome_id="mission-binding",
        repository_revision=discovery.binding.base_revision,
        tasks=(task,),
        fingerprint="a" * 64,
        discovery_fingerprint=discovery.fingerprint,
        registry=None if omit_registry else registry,
        tool_bindings=() if omit_registry else tool_bindings,
        organization_binding=PlanOrganizationBinding(
            organization_id=mission.organization_id,
            organization_version=mission.organization_version,
            organization_fingerprint=load_canonical_organization().fingerprint,
            mission_id=mission.mission_id,
            mission_origin_id=mission.origin.origin_id,
        ),
    )
    runs.accept_plan(run.run_id, plan)
    assignment = MissionTaskAssignment(
        mission_id=mission.mission_id,
        crew_version=crew.version,
        task_id="execute-work",
        accountable_owner="Backend_Service_Engineer",
        assignment_kind=CrewAssignmentKind.PRODUCTION,
        expected_result="A bounded result produced with exact tool authority",
        completion_criteria=("the result is reviewable",),
        execution_run_id=run.run_id,
        execution_task_id=task.task_id,
        authority_scope=("repository:binding",),
        exact_tools=task.tools,
        path_scopes=("repository:binding",),
        limits=(MissionResourceLimit(name="wall_time", value=120, unit="seconds"),),
        required_evidence=("artifact:result",),
        requires_independent_evaluation=requires_independent_evaluation,
    )
    missions.record_assignment(assignment)
    return missions, MissionRunBinding(
        mission_id=mission.mission_id,
        binding_key="execute-work",
        mission_task_id=assignment.task_id,
        assignment_id=assignment.assignment_id,
        assignment_revision=assignment.assignment_revision,
        run_id=run.run_id,
        execution_task_id=task.task_id,
        plan_fingerprint=plan.fingerprint,
        execution_context=PlanExecutionContext.from_binding(discovery.binding),
        authority_scope=assignment.authority_scope,
        path_scopes=assignment.path_scopes,
        recorded_by="Mission_Lead",
    )


def test_mission_run_binding_requires_immutable_tool_identity_and_version(
    tmp_path: Path,
) -> None:
    missions, missing_registry = _run_binding_fixture(
        tmp_path / "missing", tool_id="file.read", omit_registry=True
    )
    with pytest.raises(MishkanError, match="registry snapshot") as absent:
        missions.record_run_binding(missing_registry)
    assert absent.value.envelope.code is ErrorCode.PLAN

    missions, drifted_version = _run_binding_fixture(
        tmp_path / "drift", tool_id="file.read", corrupt_tool_version=True
    )
    with pytest.raises(MishkanError, match="identity or version") as drift:
        missions.record_run_binding(drifted_version)
    assert drift.value.envelope.code is ErrorCode.PLAN


def test_workspace_changing_production_requires_independent_evaluation(
    tmp_path: Path,
) -> None:
    missions, binding = _run_binding_fixture(
        tmp_path,
        tool_id="core.process.exec",
        requires_independent_evaluation=False,
    )

    with pytest.raises(MishkanError, match="independent downstream evaluation") as conflict:
        missions.record_run_binding(binding)

    assert conflict.value.envelope.code is ErrorCode.ROLE_CONFLICT


def test_mission_run_settlement_rejects_legacy_unattributed_review(tmp_path: Path) -> None:
    missions, pending = _run_binding_fixture(tmp_path, tool_id="file.read")
    missions.record_run_binding(pending)
    runs = LocalRunRepository(tmp_path / "mishkan.db")
    runs.start_run(pending.run_id)
    runs.claim_task(pending.run_id, pending.execution_task_id)
    runs.mark_validating(pending.run_id, pending.execution_task_id)
    runs.accept_result(
        pending.run_id,
        InitializationResult(
            repository_revision=pending.execution_context.repository_revision,
            task_id=pending.execution_task_id,
            summary="The mission result has repository evidence.",
            cited_paths=("README.md",),
            findings=("The exact repository README was observed.",),
        ),
        ReviewDecision(
            task_id=pending.execution_task_id,
            verdict="accepted",
            summary="A legacy review omitted accountable identities.",
            checked_citations=("README.md",),
        ),
    )
    settled = pending.model_copy(
        update={
            "binding_id": new_id(),
            "binding_revision": 2,
            "result_references": (f"run-result:{pending.run_id}:{pending.execution_task_id}",),
            "acceptance_references": (
                f"run-acceptance:{pending.run_id}:{pending.execution_task_id}",
            ),
            "acceptance": MissionRunAcceptance.ACCEPTED,
        }
    )

    with pytest.raises(MishkanError, match="separated producer and evaluator") as conflict:
        missions.record_run_binding(settled)

    assert conflict.value.envelope.code is ErrorCode.ROLE_CONFLICT


def test_multi_repository_mission_binds_exact_runs_dependencies_and_acceptance(
    tmp_path: Path,
) -> None:
    database, missions, mission = _setup(tmp_path)
    brief = _brief(mission)
    missions.record_brief(brief, expected_revision=mission.revision)
    current = missions.mission(str(mission.mission_id))
    crew = _crew(brief)
    missions.record_crew(crew, expected_revision=current.revision)
    organization = load_canonical_organization()
    runs = LocalRunRepository(database)

    pending_bindings: list[MissionRunBinding] = []
    for index, repository_name in enumerate(("api-service", "worker-service"), start=1):
        discovery = _repository_discovery(tmp_path, repository_name)
        run = runs.start_or_resume(
            discovery,
            f"Deliver mission work in {repository_name}",
            f"mission-{repository_name}",
        )
        execution_task_id = f"change-repository-{index}"
        mission_task_id = f"deliver-{repository_name}"
        task = PlanTask(
            task_id=execution_task_id,
            title=f"Change {repository_name}",
            purpose=f"Implement the accepted mission scope in {repository_name}.",
            assigned_role="Backend_Service_Engineer",
            tools=("repository.read_file",),
            evidence_paths=("README.md",),
        )
        registry, tool_bindings = resolved_tool_lineage(discovery.binding.root, (task,))
        plan = AcceptedPlan(
            objective=f"Deliver mission work in {repository_name}",
            outcome_id=f"mission-{repository_name}",
            repository_revision=discovery.binding.base_revision,
            tasks=(task,),
            fingerprint=(str(index) * 64),
            discovery_fingerprint=discovery.fingerprint,
            registry=registry,
            tool_bindings=tool_bindings,
            organization_binding=PlanOrganizationBinding(
                organization_id=organization.organization_id,
                organization_version=organization.organization_version,
                organization_fingerprint=organization.fingerprint,
                mission_id=mission.mission_id,
                mission_origin_id=mission.origin.origin_id,
            ),
        )
        runs.accept_plan(run.run_id, plan)
        scope = (f"repository:{repository_name}",)
        assignment = MissionTaskAssignment(
            mission_id=mission.mission_id,
            crew_version=crew.version,
            task_id=mission_task_id,
            accountable_owner="Backend_Service_Engineer",
            assignment_kind=CrewAssignmentKind.PRODUCTION,
            expected_result=f"A verified change in {repository_name}",
            completion_criteria=("independent task result accepted",),
            dependencies=(("deliver-api-service",) if index == 2 else ()),
            execution_run_id=run.run_id,
            execution_task_id=execution_task_id,
            authority_scope=scope,
            exact_tools=task.tools,
            path_scopes=scope,
            limits=(MissionResourceLimit(name="timeout", value=120, unit="seconds"),),
            required_evidence=("accepted run result",),
        )
        missions.record_assignment(assignment)
        binding_key = f"repo-{index}"
        pending = MissionRunBinding(
            mission_id=mission.mission_id,
            binding_key=binding_key,
            mission_task_id=mission_task_id,
            assignment_id=assignment.assignment_id,
            assignment_revision=assignment.assignment_revision,
            run_id=run.run_id,
            execution_task_id=execution_task_id,
            plan_fingerprint=plan.fingerprint,
            execution_context=PlanExecutionContext.from_binding(discovery.binding),
            depends_on_binding_keys=(("repo-1",) if index == 2 else ()),
            authority_scope=scope,
            path_scopes=scope,
            recorded_by="Backend_Service_Engineer",
        )
        assert missions.record_run_binding(pending) == pending
        pending_bindings.append(pending)

    first = pending_bindings[0]
    runs.start_run(first.run_id)
    runs.claim_task(first.run_id, first.execution_task_id)
    runs.mark_validating(first.run_id, first.execution_task_id)
    result = InitializationResult(
        repository_revision=first.execution_context.repository_revision,
        task_id=first.execution_task_id,
        summary="The API repository change has accepted evidence.",
        cited_paths=("README.md",),
        findings=("The exact repository context was independently reviewed.",),
    )
    runs.accept_result(
        first.run_id,
        result,
        ReviewDecision(
            schema_version="1.1",
            task_id=first.execution_task_id,
            producer_identity="Backend_Service_Engineer",
            evaluator_identity="Product_Functional_Evaluator",
            verdict="accepted",
            summary="Independent review accepted the repository result.",
            checked_citations=("README.md",),
        ),
    )
    settled = first.model_copy(
        update={
            "binding_id": new_id(),
            "binding_revision": 2,
            "result_references": (f"run-result:{first.run_id}:{first.execution_task_id}",),
            "acceptance_references": (f"run-acceptance:{first.run_id}:{first.execution_task_id}",),
            "acceptance": MissionRunAcceptance.ACCEPTED,
        }
    )
    assert missions.record_run_binding(settled) == settled

    durable = SQLiteMissionRepository(database).run_bindings(str(mission.mission_id))
    assert durable == (first, settled, pending_bindings[1])
    assert durable[2].depends_on_binding_keys == ("repo-1",)
    assert durable[0].execution_context.context_id != durable[2].execution_context.context_id
