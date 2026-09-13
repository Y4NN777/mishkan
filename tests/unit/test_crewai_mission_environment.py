from pathlib import Path

import pytest

from mishkan.crewai.mission_environment import (
    CrewAIMissionEnvironmentPlanningRunner,
    MissionEnvironmentDecisionOutput,
    MissionEnvironmentPlanningOutput,
)
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.environment import (
    EnvironmentObservation,
    EnvironmentObservationRequest,
    EnvironmentObserver,
    EnvironmentOutcome,
    load_environment_profile,
)
from mishkan.missions import (
    CrewAssignmentKind,
    CrewSelectionEvidence,
    ExecutiveConfirmation,
    MissionBrief,
    MissionBriefStatus,
    MissionCrewMember,
    MissionCrewRevision,
    MissionEnvironmentAlternative,
    MissionEnvironmentContextRequest,
    MissionEnvironmentIntent,
    MissionEnvironmentPlanningRequest,
    MissionEnvironmentPlanValidator,
    MissionOrigin,
    MissionOriginKind,
    MissionRecord,
    MissionResourceLimit,
    MissionTaskAssignment,
)
from mishkan.organization import load_canonical_organization


def _mission_contracts() -> tuple[
    MissionRecord,
    MissionBrief,
    MissionCrewRevision,
    tuple[MissionTaskAssignment, ...],
]:
    organization = load_canonical_organization()
    mission = MissionRecord(
        revision=4,
        origin=MissionOrigin(
            kind=MissionOriginKind.CEO,
            actor_id="CEO",
            objective="Create an isolated build environment",
        ),
        organization_id=organization.organization_id,
        organization_version=organization.organization_version,
        current_brief_version=1,
        current_crew_version=1,
    )
    pm = ExecutiveConfirmation(
        identity_id="PM",
        disposition="confirmed",
        rationale="Product requirements are covered",
        evidence_references=("evidence:product",),
        coverage=("product",),
    )
    cto = ExecutiveConfirmation(
        identity_id="CTO",
        disposition="confirmed",
        rationale="Technical requirements are covered",
        evidence_references=("evidence:technical",),
        coverage=("technical", "security", "quality"),
    )
    identities = (
        ("Backend_Service_Engineer", CrewAssignmentKind.PRODUCTION),
        ("Product_Functional_Evaluator", CrewAssignmentKind.EVALUATION),
        ("Technical_Change_Reporter", CrewAssignmentKind.REPORTING),
    )
    members = tuple(
        MissionCrewMember(
            identity_id=identity,
            assignment_kind=kind,
            responsibility=f"Own attributable {kind.value} work",
            selection_evidence=CrewSelectionEvidence(
                project_references=("repository:api",),
                competence_references=(f"profile:{identity}:competence",),
                availability_references=(f"profile:{identity}:availability",),
                conflict_assessment="No responsibility conflict",
                risk_coverage=("environment",),
                independence_references=(f"profile:{identity}:independence",),
            ),
        )
        for identity, kind in identities
    )
    brief = MissionBrief(
        mission_id=mission.mission_id,
        version=1,
        organization_id=mission.organization_id,
        organization_version=mission.organization_version,
        status=MissionBriefStatus.CONFIRMED,
        objective=mission.origin.objective,
        problem="The project has no reproducible build environment",
        desired_outcome="A verified isolated environment",
        scope=("repository:api",),
        exclusions=("production deployment",),
        acceptance_criteria=("build passes in the selected environment",),
        constraints=("preserve project files",),
        risks=("toolchain drift",),
        authority_scope=("repository:api",),
        proposed_crew=tuple(item[0] for item in identities),
        evidence_requirements=("environment observation",),
        escalation_conditions=("no compatible environment",),
        environment_intent=MissionEnvironmentIntent(
            required_evidence=("build and cleanup evidence",),
            environment_dependent=True,
        ),
        pm_confirmation=pm,
        cto_confirmation=cto,
    )
    crew = MissionCrewRevision(
        mission_id=mission.mission_id,
        version=1,
        organization_id=mission.organization_id,
        organization_version=mission.organization_version,
        brief_version=1,
        mission_lead_id="Backend_Service_Engineer",
        members=members,
        pm_composition_confirmation_id=pm.confirmation_id,
        cto_coverage_confirmation_id=cto.confirmation_id,
        revision_reason="Contextual environment mission crew",
    )
    assignments = tuple(
        MissionTaskAssignment(
            mission_id=mission.mission_id,
            crew_version=1,
            task_id=task_id,
            accountable_owner=owner,
            assignment_kind=CrewAssignmentKind.PRODUCTION,
            expected_result=expected,
            completion_criteria=("result is independently verifiable",),
            authority_scope=("repository:api",),
            exact_tools=(),
            path_scopes=("repository:api",),
            limits=(MissionResourceLimit(name="wall_time", value=600, unit="seconds"),),
            required_evidence=("artifact:result",),
            environment_context_ids=(("repository:api",) if task_id == "build-project" else ()),
        )
        for task_id, owner, expected in (
            (
                "plan-environment",
                "Backend_Service_Engineer",
                "An attributable environment decision",
            ),
            ("build-project", "Backend_Service_Engineer", "A verified project build"),
        )
    )
    return mission, brief, crew, assignments


def _observation(tmp_path: Path) -> EnvironmentObservation:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    podman = binaries / "podman"
    podman.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    podman.chmod(0o755)
    profile = load_environment_profile(
        "package://mishkan.resources.environment/default.yaml", tmp_path
    )
    return EnvironmentObserver(profile).observe(
        tmp_path,
        request=EnvironmentObservationRequest(
            actor_identity="Backend_Service_Engineer",
            context_id="repository:api",
            execution_location="local:test",
            repository_id="api",
            repository_revision="abc123",
        ),
        path_value=str(binaries),
    )


def _request(
    tmp_path: Path,
) -> tuple[
    MissionEnvironmentPlanningRequest,
    MissionRecord,
    MissionBrief,
    MissionCrewRevision,
    tuple[MissionTaskAssignment, ...],
    EnvironmentObservation,
]:
    mission, brief, crew, assignments = _mission_contracts()
    observation = _observation(tmp_path)
    alternatives = (
        MissionEnvironmentAlternative(
            alternative_id="podman-containerfile",
            requested_outcome=EnvironmentOutcome.GENERATE,
            required_semantics=("oci.build",),
            allowed_descriptor_formats=("containerfile",),
            required_engine_ids=("podman",),
            eligible_engine_ids=("podman",),
            constraints=("use an immutable base",),
            evidence_references=("observation:podman",),
        ),
        MissionEnvironmentAlternative(
            alternative_id="unresolved",
            requested_outcome=EnvironmentOutcome.UNRESOLVED,
            evidence_references=("observation:podman",),
        ),
    )
    request = MissionEnvironmentPlanningRequest(
        mission_id=mission.mission_id,
        mission_revision=mission.revision,
        brief_version=brief.version,
        crew_version=crew.version,
        planning_task_id="plan-environment",
        owner_identity="Backend_Service_Engineer",
        contexts=(
            MissionEnvironmentContextRequest(
                context_id=observation.context_id,
                observation_id=observation.observation_id,
                observation_revision=observation.revision,
                observation_fingerprint=observation.fingerprint,
                target_platform=observation.platform,
                target_architecture=observation.architecture,
                execution_location=observation.execution_location,
                affected_task_ids=("build-project",),
                alternatives=alternatives,
                evidence_references=("observation:podman",),
            ),
        ),
        evidence_references=("brief:environment-intent",),
    )
    return request, mission, brief, crew, assignments, observation


def _output() -> MissionEnvironmentPlanningOutput:
    return MissionEnvironmentPlanningOutput(
        decisions=(
            MissionEnvironmentDecisionOutput(
                context_id="repository:api",
                selected_alternative_id="podman-containerfile",
                requested_outcome="generate",
                rationale="Podman is observed eligible and supplies the required build semantics",
                constraints=("do not persist before approval",),
                declared_effects=("descriptor.generate", "container.image.build"),
                verification_checks=("build", "project tests"),
                cleanup_criteria=("remove temporary image",),
                alternatives_considered=("podman-containerfile", "unresolved"),
                unknowns=(),
                evidence_references=("observation:podman",),
            ),
        )
    )


def test_crewai_output_compiles_to_exact_agent_authored_constraints(tmp_path: Path) -> None:
    request, mission, brief, crew, assignments, observation = _request(tmp_path)
    plan = CrewAIMissionEnvironmentPlanningRunner.compile(
        request, _output(), plan_version=1, model_route="planning"
    )
    profile = load_environment_profile(
        "package://mishkan.resources.environment/default.yaml", tmp_path
    )

    MissionEnvironmentPlanValidator.validate_plan(
        plan,
        mission=mission,
        brief=brief,
        crew=crew,
        assignments=assignments,
        observations=(observation,),
        profile=profile,
    )
    binding = plan.binding_request("repository:api", policy_fingerprint="a" * 64)

    assert plan.lineage.runtime == "crewai-1.x"
    assert plan.owner_identity == "Backend_Service_Engineer"
    assert binding.requested_outcome is EnvironmentOutcome.GENERATE
    assert binding.required_engine_ids == ("podman",)
    assert binding.allowed_descriptor_formats == ("containerfile",)


def test_environment_planner_cannot_choose_an_unexposed_outcome(tmp_path: Path) -> None:
    request, *_rest = _request(tmp_path)
    invalid = _output().model_copy(
        update={
            "decisions": (
                _output().decisions[0].model_copy(update={"requested_outcome": "host_native"}),
            )
        }
    )

    with pytest.raises(MishkanError) as error:
        CrewAIMissionEnvironmentPlanningRunner.compile(
            request, invalid, plan_version=1, model_route="planning"
        )
    assert error.value.envelope.code is ErrorCode.PLAN


def test_independent_evaluator_cannot_own_environment_production_plan(
    tmp_path: Path,
) -> None:
    request, mission, brief, crew, assignments, observation = _request(tmp_path)
    changed = request.model_copy(
        update={
            "owner_identity": "Product_Functional_Evaluator",
            "planning_task_id": "build-project",
        }
    )
    changed_assignment = assignments[1].model_copy(
        update={"accountable_owner": "Product_Functional_Evaluator"}
    )

    with pytest.raises(MishkanError) as error:
        MissionEnvironmentPlanValidator.validate_request(
            changed,
            mission=mission,
            brief=brief,
            crew=crew,
            assignments=(assignments[0], changed_assignment),
            observations=(observation,),
        )
    assert error.value.envelope.code is ErrorCode.ROLE_CONFLICT
