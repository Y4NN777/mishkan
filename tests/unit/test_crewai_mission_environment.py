from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from mishkan.config.loader import ConfigLoader
from mishkan.config.models import MishkanConfig
from mishkan.config.presets import preset_text
from mishkan.conversations import (
    DecisionAlternative,
    DecisionContext,
    DecisionContextElement,
    DecisionCriterion,
    DecisionCriterionAssessment,
    DecisionEvidenceClaim,
    DecisionEvidenceClass,
    DecisionRecommendation,
    DecisionStatus,
    DecisionValidation,
    DecisionValidationStatus,
    MissionDecision,
)
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


def _config(tmp_path: Path) -> MishkanConfig:
    source = tmp_path / "config.yaml"
    source.write_text(preset_text("local"), encoding="utf-8")
    return ConfigLoader().load([source]).value


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
        coverage=("product", "developer-experience", "composition"),
    )
    cto = ExecutiveConfirmation(
        identity_id="CTO",
        disposition="confirmed",
        rationale="Technical requirements are covered",
        evidence_references=("evidence:technical",),
        coverage=("technical", "platform", "security", "quality", "operability"),
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


def _accepted_environment_decision(
    mission: MissionRecord,
    *,
    selected_option: str,
) -> MissionDecision:
    evidence = "observation:podman"
    criterion = DecisionCriterion(
        criterion_id="mission-fit",
        description="Satisfies the mission environment constraints",
        provenance_references=("requirement:environment",),
    )

    def alternative(option_id: str, description: str) -> DecisionAlternative:
        return DecisionAlternative(
            option_id=option_id,
            description=description,
            credible=True,
            assessments=(
                DecisionCriterionAssessment(
                    criterion_id=criterion.criterion_id,
                    assessment=f"Evidence assessment for {option_id}",
                    evidence_references=(evidence,),
                ),
            ),
        )

    return MissionDecision(
        schema_version="1.1",
        mission_id=mission.mission_id,
        conversation_id=uuid4(),
        actor_id="CTO",
        producer_identity="Backend_Service_Engineer",
        deciding_identity="CTO",
        subject="Mission environment architecture",
        disposition=DecisionStatus.ACCEPTED.value,
        decision_status=DecisionStatus.ACCEPTED,
        reason="Independent validation accepted the environment choice",
        scope=("environment:repository:api", "task:build-project"),
        evidence_references=(evidence,),
        authority_reference="authority:mission-environment",
        changes_durable_authority=True,
        context=DecisionContext(
            question="Which environment should the mission adopt?",
            objective_reference="objective:mission",
            effective_policy_reference="policy:effective",
            requirements=(
                DecisionContextElement(
                    statement="The build must be isolated",
                    provenance_reference="requirement:environment",
                ),
            ),
            repository_evidence=(
                DecisionContextElement(
                    statement="Podman is observed on the target",
                    provenance_reference=evidence,
                ),
            ),
            constraints=(
                DecisionContextElement(
                    statement="Preserve the project workspace",
                    provenance_reference="constraint:workspace",
                ),
            ),
            declared_preferences=(
                DecisionContextElement(
                    statement="Prefer a reproducible local environment",
                    provenance_reference="preference:local",
                ),
            ),
            risks=(
                DecisionContextElement(
                    statement="Container compatibility can drift",
                    provenance_reference="risk:compatibility",
                ),
            ),
            material_unknowns=(
                DecisionContextElement(
                    statement="The final base image remains unselected",
                    provenance_reference="unknown:base-image",
                ),
            ),
        ),
        evidence=(
            DecisionEvidenceClaim(
                claim="Podman is available on the target",
                classification=DecisionEvidenceClass.VERIFIED,
                source_reference=evidence,
            ),
        ),
        criteria=(criterion,),
        alternatives=(
            alternative("podman-containerfile", "Generate a Podman Containerfile"),
            alternative("host-native", "Use the observed host toolchain"),
        ),
        alternatives_search="Compared isolated generation with host-native execution",
        recommendation=DecisionRecommendation(
            recommended_option_id=selected_option,
            rationale="The selected option best satisfies the mission constraints",
            tradeoffs=("It adds environment lifecycle work",),
            risks=("Compatibility still requires verification",),
            confidence=0.8,
            confidence_basis="The engine observation is attributable",
            unresolved_questions=("Which immutable base image should be used?",),
            expected_consequences=("The project gains an isolated build environment",),
            reversal_or_migration=("Remove the descriptor and return to host-native execution",),
        ),
        validation=DecisionValidation(
            validation_type="independent compatibility review",
            planned_evidence=("Verify target compatibility",),
            status=DecisionValidationStatus.PASSED,
            evaluator_identity="Software_Technical_Evaluator",
            evidence_references=(evidence,),
            findings=("The choice is compatible with the observed target",),
        ),
        supersedes_decision_id=uuid4(),
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


def test_environment_proposal_runs_the_assigned_mission_agent_through_crewai(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, mission, brief, crew, _assignments, observation = _request(tmp_path)
    runner = CrewAIMissionEnvironmentPlanningRunner(_config(tmp_path))
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runner._models,
        "candidates_for",
        lambda route_name: (SimpleNamespace(route_name=route_name),),
    )

    def crew_result(role, llm, _description):  # type: ignore[no-untyped-def]
        calls.append((role.name, llm.route_name))
        return SimpleNamespace(pydantic=_output(), raw="")

    monkeypatch.setattr(runner, "_crew", crew_result)

    plan = runner.propose(
        request,
        mission=mission,
        brief=brief,
        crew=crew,
        observations=(observation,),
        plan_version=1,
    )

    assert calls == [
        (request.owner_identity, runner._config.crewai.mission_environment_model_route)
    ]
    assert plan.lineage.runtime == "crewai-1.x"
    assert plan.lineage.output_fingerprint
    assert plan.owner_identity == request.owner_identity


def test_consequential_environment_choice_requires_accepted_pln_decision(
    tmp_path: Path,
) -> None:
    request, mission, *_rest = _request(tmp_path)
    accepted = _accepted_environment_decision(
        mission,
        selected_option="podman-containerfile",
    )
    context = request.contexts[0]
    selected = context.alternatives[0].model_copy(
        update={
            "requires_consequential_decision": True,
            "consequential_decision_id": accepted.decision_id,
            "consequential_option_id": "podman-containerfile",
        }
    )
    linked_request = request.model_copy(
        update={
            "contexts": (
                context.model_copy(update={"alternatives": (selected, *context.alternatives[1:])}),
            )
        }
    )

    plan = CrewAIMissionEnvironmentPlanningRunner.compile(
        linked_request,
        _output(),
        plan_version=1,
        model_route="planning",
    )

    assert plan.decisions[0].consequential_decision_id == accepted.decision_id
    with pytest.raises(MishkanError, match="no durable decision"):
        MissionEnvironmentPlanValidator.validate_consequential_decisions(plan, {})

    MissionEnvironmentPlanValidator.validate_consequential_decisions(
        plan,
        {accepted.decision_id: accepted},
    )

    wrong_option = accepted.model_copy(
        update={
            "recommendation": accepted.recommendation.model_copy(
                update={"recommended_option_id": "host-native"}
            )
        }
    )
    with pytest.raises(MishkanError, match="lacks its accepted PLN-012-018 decision"):
        MissionEnvironmentPlanValidator.validate_consequential_decisions(
            plan,
            {accepted.decision_id: wrong_option},
        )


def test_consequential_environment_alternative_requires_complete_decision_link() -> None:
    with pytest.raises(ValueError, match="requires a decision and option reference"):
        MissionEnvironmentAlternative(
            alternative_id="generated",
            requested_outcome=EnvironmentOutcome.GENERATE,
            allowed_descriptor_formats=("containerfile",),
            evidence_references=("observation:podman",),
            requires_consequential_decision=True,
        )


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
