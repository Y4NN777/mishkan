from uuid import UUID, uuid4

import pytest

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.missions import (
    CrewAssignmentKind,
    MissionAssignmentGraphValidator,
    MissionResourceLimit,
    MissionTaskAssignment,
)


def _assignment(
    mission_id: UUID,
    task_id: str,
    owner: str,
    kind: CrewAssignmentKind,
    *,
    dependencies: tuple[str, ...] = (),
    requires_evaluation: bool = False,
    binding: tuple[str, str] | None = None,
) -> MissionTaskAssignment:
    return MissionTaskAssignment(
        mission_id=mission_id,
        crew_version=1,
        task_id=task_id,
        accountable_owner=owner,
        assignment_kind=kind,
        expected_result=f"An attributable {kind.value} result",
        completion_criteria=("result contract is satisfied",),
        dependencies=dependencies,
        execution_run_id=binding[0] if binding is not None else None,
        execution_task_id=binding[1] if binding is not None else None,
        authority_scope=("repository:api",),
        exact_tools=("file.read",),
        path_scopes=("repository:api",),
        limits=(MissionResourceLimit(name="wall_time", value=60, unit="seconds"),),
        required_evidence=(f"evidence:{task_id}",),
        requires_independent_evaluation=requires_evaluation,
    )


def _valid_graph() -> tuple[MissionTaskAssignment, ...]:
    mission_id = uuid4()
    return (
        _assignment(
            mission_id,
            "produce",
            "Backend_Service_Engineer",
            CrewAssignmentKind.PRODUCTION,
            requires_evaluation=True,
            binding=("run-1", "produce"),
        ),
        _assignment(
            mission_id,
            "evaluate",
            "Software_Technical_Evaluator",
            CrewAssignmentKind.EVALUATION,
            dependencies=("produce",),
            binding=("run-1", "evaluate"),
        ),
        _assignment(
            mission_id,
            "report",
            "Technical_Change_Reporter",
            CrewAssignmentKind.REPORTING,
            dependencies=("evaluate",),
            binding=("run-1", "report"),
        ),
    )


def test_production_evaluation_and_reporting_chain_is_valid() -> None:
    MissionAssignmentGraphValidator.validate(_valid_graph())


def test_production_requiring_assurance_cannot_skip_independent_evaluation() -> None:
    production, _evaluation, reporting = _valid_graph()
    direct_reporting = reporting.model_copy(update={"dependencies": (production.task_id,)})

    with pytest.raises(MishkanError) as error:
        MissionAssignmentGraphValidator.validate((production, direct_reporting))

    assert error.value.envelope.code is ErrorCode.ROLE_CONFLICT
    assert "independent evaluation" in error.value.envelope.message


def test_multi_task_mission_cannot_skip_reporting() -> None:
    production, evaluation, _reporting = _valid_graph()

    with pytest.raises(MishkanError) as error:
        MissionAssignmentGraphValidator.validate((production, evaluation))

    assert error.value.envelope.code is ErrorCode.ROLE_CONFLICT
    assert "reporting" in error.value.envelope.message


def test_assignment_graph_rejects_cycles_and_duplicate_run_bindings() -> None:
    production, evaluation, reporting = _valid_graph()
    cyclic_production = production.model_copy(update={"dependencies": (reporting.task_id,)})
    duplicate_binding = reporting.model_copy(
        update={
            "execution_run_id": evaluation.execution_run_id,
            "execution_task_id": evaluation.execution_task_id,
        }
    )

    with pytest.raises(MishkanError) as cycle:
        MissionAssignmentGraphValidator.validate((cyclic_production, evaluation, reporting))
    with pytest.raises(MishkanError) as duplicate:
        MissionAssignmentGraphValidator.validate((production, evaluation, duplicate_binding))

    assert cycle.value.envelope.code is ErrorCode.PLAN
    assert duplicate.value.envelope.code is ErrorCode.PLAN
