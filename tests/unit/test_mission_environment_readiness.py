from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from mishkan.environment import (
    AvailabilityState,
    EnvironmentBinding,
    EnvironmentBindingState,
    EnvironmentOutcome,
    EnvironmentSettlement,
    EnvironmentVerification,
)
from mishkan.missions import (
    CrewAIPlanningLineage,
    CrewAssignmentKind,
    EnvironmentReadinessState,
    MissionEnvironmentAlternative,
    MissionEnvironmentContextRequest,
    MissionEnvironmentDecision,
    MissionEnvironmentPlan,
    MissionEnvironmentPlanAcceptance,
    MissionEnvironmentReadinessService,
    MissionOrigin,
    MissionOriginKind,
    MissionRecord,
    MissionResourceLimit,
    MissionTaskAssignment,
)


@dataclass
class _Missions:
    mission_record: MissionRecord
    assignment_records: tuple[MissionTaskAssignment, ...]
    acceptance: MissionEnvironmentPlanAcceptance | None

    def mission(self, mission_id: str) -> MissionRecord:
        assert mission_id == str(self.mission_record.mission_id)
        return self.mission_record

    def assignments(
        self, mission_id: str, *, limit: int = 1_000
    ) -> tuple[MissionTaskAssignment, ...]:
        assert mission_id == str(self.mission_record.mission_id)
        assert limit == 1_000
        return self.assignment_records

    def environment_plan(
        self,
        mission_id: str,
        version: int | None = None,
    ) -> MissionEnvironmentPlanAcceptance:
        assert mission_id == str(self.mission_record.mission_id)
        assert version is None
        assert self.acceptance is not None
        return self.acceptance


@dataclass
class _Environments:
    binding: EnvironmentBinding | None = None
    verifications: list[EnvironmentVerification] = field(default_factory=list)

    def binding_for_request(self, request_id: str) -> EnvironmentBinding | None:
        if self.binding is not None:
            assert request_id == str(self.binding.request.request_id)
        return self.binding

    def verifications_for_binding(
        self,
        binding_id: str,
        *,
        limit: int = 1_000,
    ) -> tuple[EnvironmentVerification, ...]:
        assert self.binding is not None
        assert binding_id == str(self.binding.binding_id)
        assert limit == 1_000
        return tuple(self.verifications)


def _fixture() -> tuple[
    _Missions,
    _Environments,
    MissionEnvironmentPlanAcceptance,
    EnvironmentBinding,
]:
    observation_id = uuid4()
    mission = MissionRecord(
        revision=4,
        origin=MissionOrigin(
            kind=MissionOriginKind.CEO,
            actor_id="CEO",
            objective="Build only after current environment evidence is verified",
        ),
        organization_id="mishkan",
        organization_version="1",
        current_brief_version=1,
        current_crew_version=1,
        current_environment_plan_version=1,
    )
    planning = MissionTaskAssignment(
        mission_id=mission.mission_id,
        crew_version=1,
        task_id="plan-environment",
        accountable_owner="Platform_Engineer",
        assignment_kind=CrewAssignmentKind.PRODUCTION,
        expected_result="An attributable environment decision",
        completion_criteria=("decision is accepted",),
        authority_scope=("repository:api",),
        exact_tools=(),
        path_scopes=("repository:api",),
        limits=(MissionResourceLimit(name="wall_time", value=600, unit="seconds"),),
        required_evidence=("observation:api",),
    )
    build = planning.model_copy(
        update={
            "assignment_id": uuid4(),
            "task_id": "build-project",
            "expected_result": "A verified build",
            "environment_context_ids": ("repository:api",),
        }
    )
    alternative = MissionEnvironmentAlternative(
        alternative_id="host-native",
        requested_outcome=EnvironmentOutcome.HOST_NATIVE,
        required_semantics=("process",),
        required_engine_ids=("python",),
        eligible_engine_ids=("python",),
        evidence_references=("observation:api",),
    )
    context = MissionEnvironmentContextRequest(
        context_id="repository:api",
        observation_id=observation_id,
        observation_revision=1,
        observation_fingerprint="a" * 64,
        target_platform="linux",
        target_architecture="x86_64",
        execution_location="local:test",
        affected_task_ids=("build-project",),
        alternatives=(alternative,),
        evidence_references=("observation:api",),
    )
    plan = MissionEnvironmentPlan(
        mission_id=mission.mission_id,
        version=1,
        mission_revision=3,
        brief_version=1,
        crew_version=1,
        source_request_id=uuid4(),
        planning_task_id="plan-environment",
        owner_identity="Platform_Engineer",
        evidence_references=("brief:environment",),
        contexts=(context,),
        decisions=(
            MissionEnvironmentDecision(
                context_id=context.context_id,
                selected_alternative_id=alternative.alternative_id,
                requested_outcome=alternative.requested_outcome,
                rationale="The observed host toolchain satisfies the declared isolation",
                constraints=(),
                declared_effects=(),
                verification_checks=("project-check",),
                cleanup_criteria=("no resources remain",),
                alternatives_considered=(alternative.alternative_id,),
                unknowns=(),
                evidence_references=("observation:api",),
                required_semantics=alternative.required_semantics,
                required_engine_ids=alternative.required_engine_ids,
                eligible_engine_ids=alternative.eligible_engine_ids,
            ),
        ),
        lineage=CrewAIPlanningLineage(
            model_route="local",
            output_fingerprint="b" * 64,
        ),
    )
    acceptance = MissionEnvironmentPlanAcceptance(
        plan=plan,
        accepted_by="PM",
        policy_fingerprint="c" * 64,
    )
    request = plan.binding_request(
        context.context_id,
        policy_fingerprint=acceptance.policy_fingerprint,
    )
    binding = EnvironmentBinding(
        request=request,
        state=EnvironmentBindingState.COMPATIBLE,
        selected_engine_ids=("python",),
        selected_adapter_ids=("host.python",),
        selected_descriptors=(),
        missing_conditions=(),
        lost_fidelity=(),
        reason="exact requested host-native outcome is compatible",
    )
    return _Missions(mission, (planning, build), acceptance), _Environments(), acceptance, binding


def test_environment_dependent_task_waits_for_binding_and_verification() -> None:
    missions, environments, _acceptance, binding = _fixture()
    service = MissionEnvironmentReadinessService(missions, environments)

    awaiting_binding = service.inspect(str(missions.mission_record.mission_id))
    environments.binding = binding
    awaiting_verification = service.inspect(str(missions.mission_record.mission_id))
    environments.verifications.append(
        EnvironmentVerification(
            binding_id=binding.binding_id,
            context_fingerprint=binding.request.observation_fingerprint,
            location_fingerprint="d" * 64,
            engine_id="python",
            checks={"project-check": AvailabilityState.TRUE},
            attempt_ids=(UUID("00000000-0000-4000-8000-000000000001"),),
            artifact_references=(),
            settlement=EnvironmentSettlement.VERIFIED,
            limitations=(),
        )
    )
    verified = service.inspect(str(missions.mission_record.mission_id))

    assert awaiting_binding.blocked_task_ids == ("build-project",)
    assert awaiting_binding.tasks[0].state is EnvironmentReadinessState.AWAITING_BINDING
    assert awaiting_verification.tasks[0].state is EnvironmentReadinessState.AWAITING_VERIFICATION
    assert verified.ready_task_ids == ("build-project", "plan-environment")
    assert verified.tasks[0].state is EnvironmentReadinessState.ELIGIBLE


def test_stale_binding_pauses_only_its_declared_dependent_task() -> None:
    missions, environments, _acceptance, binding = _fixture()
    environments.binding = binding.model_copy(
        update={
            "revision": 2,
            "state": EnvironmentBindingState.STALE,
            "selected_engine_ids": (),
            "selected_adapter_ids": (),
            "missing_conditions": ("invalidation:repository",),
            "reason": "repository revision changed",
        }
    )

    readiness = MissionEnvironmentReadinessService(missions, environments).inspect(
        str(missions.mission_record.mission_id)
    )

    assert readiness.blocked_task_ids == ("build-project",)
    assert readiness.ready_task_ids == ("plan-environment",)
    assert readiness.tasks[0].state is EnvironmentReadinessState.STALE


def test_environment_plan_from_an_older_brief_or_crew_blocks_dependent_tasks() -> None:
    missions, environments, _acceptance, binding = _fixture()
    environments.binding = binding
    missions.mission_record = missions.mission_record.model_copy(
        update={"current_brief_version": 2}
    )

    readiness = MissionEnvironmentReadinessService(missions, environments).inspect(
        str(missions.mission_record.mission_id)
    )

    assert readiness.environment_plan_version is None
    assert readiness.blocked_task_ids == ("build-project",)
    assert readiness.ready_task_ids == ("plan-environment",)
    assert readiness.tasks[0].state is EnvironmentReadinessState.AWAITING_PLAN
