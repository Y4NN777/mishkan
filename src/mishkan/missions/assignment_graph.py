"""Structural validation for contextual mission assignments."""

from __future__ import annotations

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.missions.models import CrewAssignmentKind, MissionTaskAssignment


class MissionAssignmentGraphValidator:
    """Enforce separation and evidence flow without defining a workflow catalogue."""

    @classmethod
    def validate(cls, assignments: tuple[MissionTaskAssignment, ...]) -> None:
        latest = cls.latest(assignments)
        if not latest:
            raise MishkanError(ErrorCode.MISSION, "mission has no accountable task assignments")
        task_ids = set(latest)
        for assignment in latest.values():
            unknown = set(assignment.dependencies) - task_ids
            if unknown:
                raise MishkanError(
                    ErrorCode.PLAN,
                    "mission assignment references unknown dependencies",
                    details={
                        "task_id": assignment.task_id,
                        "unknown_task_ids": sorted(unknown),
                    },
                )
        cls._require_acyclic(latest)
        ancestors = {task_id: cls._ancestors(task_id, latest) for task_id in latest}
        evaluation_tasks = {
            task_id
            for task_id, assignment in latest.items()
            if assignment.assignment_kind is CrewAssignmentKind.EVALUATION
        }
        reporting_tasks = {
            task_id
            for task_id, assignment in latest.items()
            if assignment.assignment_kind is CrewAssignmentKind.REPORTING
        }
        production_tasks = {
            task_id
            for task_id, assignment in latest.items()
            if assignment.assignment_kind is CrewAssignmentKind.PRODUCTION
        }
        for task_id in evaluation_tasks:
            if not ancestors[task_id].intersection(production_tasks):
                raise MishkanError(
                    ErrorCode.ROLE_CONFLICT,
                    "independent evaluation has no upstream production result",
                    details={"task_id": task_id},
                )
        if len(latest) > 1 and not reporting_tasks:
            raise MishkanError(
                ErrorCode.ROLE_CONFLICT,
                "multi-task mission has no separately attributable reporting task",
            )
        for task_id, assignment in latest.items():
            if not assignment.requires_independent_evaluation:
                continue
            downstream_evaluations = {
                evaluator for evaluator in evaluation_tasks if task_id in ancestors[evaluator]
            }
            if not downstream_evaluations:
                raise MishkanError(
                    ErrorCode.ROLE_CONFLICT,
                    "production work lacks a downstream independent evaluation",
                    details={"task_id": task_id},
                )
            if not any(
                downstream_evaluations.intersection(ancestors[reporter])
                for reporter in reporting_tasks
            ):
                raise MishkanError(
                    ErrorCode.ROLE_CONFLICT,
                    "independently evaluated production lacks downstream reporting",
                    details={"task_id": task_id},
                )
        bindings = [
            (assignment.execution_run_id, assignment.execution_task_id)
            for assignment in latest.values()
            if assignment.execution_run_id is not None and assignment.execution_task_id is not None
        ]
        if len(bindings) != len(set(bindings)):
            raise MishkanError(
                ErrorCode.PLAN,
                "mission assignments cannot share one durable run-task binding",
            )

    @staticmethod
    def latest(
        assignments: tuple[MissionTaskAssignment, ...],
    ) -> dict[str, MissionTaskAssignment]:
        latest: dict[str, MissionTaskAssignment] = {}
        for assignment in assignments:
            current = latest.get(assignment.task_id)
            if current is None or assignment.assignment_revision > current.assignment_revision:
                latest[assignment.task_id] = assignment
        return latest

    @classmethod
    def _require_acyclic(cls, assignments: dict[str, MissionTaskAssignment]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise MishkanError(
                    ErrorCode.PLAN, "mission assignment dependencies contain a cycle"
                )
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in assignments[task_id].dependencies:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in assignments:
            visit(task_id)

    @staticmethod
    def _ancestors(
        task_id: str,
        assignments: dict[str, MissionTaskAssignment],
    ) -> set[str]:
        ancestors: set[str] = set()
        pending = list(assignments[task_id].dependencies)
        while pending:
            dependency = pending.pop()
            if dependency in ancestors:
                continue
            ancestors.add(dependency)
            pending.extend(assignments[dependency].dependencies)
        return ancestors
