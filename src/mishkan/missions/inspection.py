"""Bounded non-authoritative drill-down projections for organization clients."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from mishkan.artifacts.service import DurableArtifactService
from mishkan.conversations import SQLiteConversationRepository
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.missions.execution import MissionTaskClaimService
from mishkan.missions.readiness import MissionEnvironmentReadinessService
from mishkan.missions.repository import SQLiteMissionRepository
from mishkan.organization import OrganizationRosterDefinition
from mishkan.persistence import LocalRunRepository, SQLiteApplicationRepository


class MissionInspectionService:
    """Join authoritative stores into bounded navigation views without owning state."""

    def __init__(
        self,
        *,
        organization: OrganizationRosterDefinition,
        missions: SQLiteMissionRepository,
        conversations: SQLiteConversationRepository,
        application: SQLiteApplicationRepository,
        runs: LocalRunRepository,
        artifacts: DurableArtifactService,
        readiness: MissionEnvironmentReadinessService,
        task_claims: MissionTaskClaimService,
    ) -> None:
        self._organization = organization
        self._missions = missions
        self._conversations = conversations
        self._application = application
        self._runs = runs
        self._artifacts = artifacts
        self._readiness = readiness
        self._task_claims = task_claims

    def mission(self, mission_id: str, *, limit: int) -> dict[str, object]:
        self._require_limit(limit)
        mission = self._missions.mission(mission_id)
        brief = (
            self._missions.brief(mission_id) if mission.current_brief_version is not None else None
        )
        crew = self._missions.crew(mission_id) if mission.current_crew_version is not None else None
        environment_plan = (
            self._missions.environment_plan(mission_id)
            if mission.current_environment_plan_version is not None
            else None
        )
        assignments = self._missions.assignments(mission_id, limit=limit)
        run_bindings = self._missions.run_bindings(mission_id, limit=limit)
        run_reports = self._missions.run_reports(mission_id, limit=limit)
        transitions = self._missions.transitions(mission_id, limit=limit)
        channels = self._conversations.channels(mission_id=mission_id, limit=limit)
        decisions = self._conversations.decisions(mission_id, limit=limit)
        escalations = self._conversations.escalations(mission_id, limit=limit)
        interventions = self._conversations.interventions(mission_id, limit=limit)
        run_ids = tuple(dict.fromkeys(item.run_id for item in run_bindings))
        run_rows = {
            str(item["id"]): item
            for item in self._application.runs(offset=0, limit=1_000)
            if str(item["id"]) in run_ids
        }
        plans: list[dict[str, object]] = []
        results: list[dict[str, object]] = []
        reviews: list[dict[str, object]] = []
        tasks: list[dict[str, object]] = []
        failures: list[dict[str, object]] = []
        events = list(
            self._application.events(
                after_cursor=0,
                limit=limit,
                entity_type="mission",
                entity_id=mission_id,
            ).events
        )
        for run_id in run_ids:
            snapshot = self._runs.snapshot(run_id)
            if snapshot.plan is not None:
                plans.append(snapshot.plan.model_dump(mode="json"))
            results.extend(item.model_dump(mode="json") for item in snapshot.results)
            reviews.extend(item.model_dump(mode="json") for item in snapshot.reviews)
            run_tasks = self._application.tasks(run_id, offset=0, limit=limit)
            tasks.extend(run_tasks)
            if run_rows.get(run_id, {}).get("status") == "failed":
                failures.append({"kind": "run", "run_id": run_id, "state": "failed"})
            failures.extend(
                {
                    "kind": "task",
                    "run_id": run_id,
                    "task_id": item["task_id"],
                    "state": item["status"],
                }
                for item in run_tasks
                if item.get("status") in {"failed", "rejected", "cancelled"}
            )
            failures.extend(
                {
                    "kind": "review_rejection",
                    "run_id": run_id,
                    "record": item.model_dump(mode="json"),
                }
                for item in self._runs.rejected_reviews(run_id)
            )
            events.extend(
                self._application.events(
                    after_cursor=0,
                    limit=limit,
                    entity_type="run",
                    entity_id=run_id,
                ).events
            )
        event_payloads = [item.model_dump(mode="json") for item in events]
        cost_observations = self._named_values(event_payloads, names={"cost", "cost_usd"})
        artifact_manifests = tuple(
            item
            for item in self._artifacts.list_manifests(offset=0, limit=1_000)
            if item.provenance.run_id in run_ids
        )[:limit]
        evidence = self._evidence_references(
            (
                brief,
                crew,
                environment_plan,
                assignments,
                run_bindings,
                run_reports,
                transitions,
                decisions,
                escalations,
                interventions,
            )
        )[:limit]
        risks = self._named_values(
            (
                brief.model_dump(mode="json") if brief is not None else {},
                *(item.model_dump(mode="json") for item in decisions),
                *(item.model_dump(mode="json") for item in escalations),
            ),
            names={"risk", "risks", "residual_risks"},
        )[:limit]
        identities_by_id = {item.identity_id: item for item in self._organization.identities}
        agent_ids = (
            tuple(dict.fromkeys(item.identity_id for item in crew.members))
            if crew is not None
            else ()
        )
        return {
            "projection": self._projection_metadata(limit),
            "mission": mission.model_dump(mode="json"),
            "brief": brief.model_dump(mode="json") if brief is not None else None,
            "crew": crew.model_dump(mode="json") if crew is not None else None,
            "agents": [
                identities_by_id[item].model_dump(mode="json")
                for item in agent_ids
                if item in identities_by_id
            ],
            "environment_plan": (
                environment_plan.model_dump(mode="json") if environment_plan is not None else None
            ),
            "readiness": self._readiness.inspect(mission_id).model_dump(mode="json"),
            "task_eligibility": [
                self._task_claims.inspect(mission_id, item.task_id).model_dump(mode="json")
                for item in {assignment.task_id: assignment for assignment in assignments}.values()
            ],
            "completion_readiness": self._task_claims.inspect_completion(mission_id).model_dump(
                mode="json"
            ),
            "runs": [run_rows[item] for item in run_ids if item in run_rows][:limit],
            "run_bindings": [item.model_dump(mode="json") for item in run_bindings],
            "run_reports": [item.model_dump(mode="json") for item in run_reports],
            "plans": plans[:limit],
            "tasks": tasks[:limit],
            "results": results[:limit],
            "reviews": reviews[:limit],
            "artifacts": [item.model_dump(mode="json") for item in artifact_manifests],
            "evidence": list(evidence),
            "risks": list(risks),
            "failures": failures[:limit],
            "costs": {
                "status": "recorded" if cost_observations else "not_recorded",
                "items": list(cost_observations[:limit]),
            },
            "schedules": {
                "status": "not_available",
                "items": [],
                "reason": "persistent scheduling is not installed in this instance",
            },
            "assignments": [item.model_dump(mode="json") for item in assignments],
            "transitions": [item.model_dump(mode="json") for item in transitions],
            "conversations": [item.model_dump(mode="json") for item in channels],
            "decisions": [item.model_dump(mode="json") for item in decisions],
            "escalations": [item.model_dump(mode="json") for item in escalations],
            "interventions": [item.model_dump(mode="json") for item in interventions],
            "events": sorted(event_payloads, key=lambda item: int(item["cursor"]))[-limit:],
        }

    def organization(self, *, limit: int) -> dict[str, object]:
        self._require_limit(limit)
        missions = self._missions.list_missions(limit=limit)
        mission_branches = self._mission_branches(missions)
        return {
            "projection": self._projection_metadata(limit),
            "organization": self._organization.model_dump(mode="json"),
            "status": {
                "mission_count": len(missions),
                "mission_states": self._counts(item.state.value for item in missions),
            },
            "branches": [
                {
                    **branch.model_dump(mode="json"),
                    "identity_count": sum(
                        item.branch_id == branch.branch_id for item in self._organization.identities
                    ),
                    "mission_ids": [
                        str(item.mission_id)
                        for item in missions
                        if branch.branch_id in mission_branches.get(str(item.mission_id), set())
                    ],
                    "drill_down": f"/v1/organization/branches/{branch.branch_id}/inspection",
                }
                for branch in self._organization.branches
            ],
            "missions": [item.model_dump(mode="json") for item in missions],
        }

    def branch(self, branch_id: str, *, limit: int) -> dict[str, object]:
        self._require_limit(limit)
        branch = next(
            (item for item in self._organization.branches if item.branch_id == branch_id),
            None,
        )
        if branch is None:
            raise MishkanError(ErrorCode.MISSION, "organization branch does not exist")
        identities = tuple(
            item for item in self._organization.identities if item.branch_id == branch_id
        )
        identity_ids = {item.identity_id for item in identities}
        pools = tuple(
            item for item in self._organization.pools if identity_ids.intersection(item.members)
        )
        missions = self._missions.list_missions(limit=1_000)
        mission_branches = self._mission_branches(missions)
        branch_missions = tuple(
            item
            for item in missions
            if branch_id in mission_branches.get(str(item.mission_id), set())
        )[:limit]
        channels = tuple(
            item
            for item in self._conversations.channels(limit=1_000)
            if item.branch_id == branch_id
        )[:limit]
        return {
            "projection": self._projection_metadata(limit),
            "branch": branch.model_dump(mode="json"),
            "status": {
                "mission_count": len(branch_missions),
                "mission_states": self._counts(item.state.value for item in branch_missions),
                "conversation_count": len(channels),
            },
            "agents": [item.model_dump(mode="json") for item in identities],
            "pools": [item.model_dump(mode="json") for item in pools],
            "missions": [item.model_dump(mode="json") for item in branch_missions],
            "conversations": [item.model_dump(mode="json") for item in channels],
            "mission_drill_down": {
                str(item.mission_id): f"/v1/missions/{item.mission_id}/inspection"
                for item in branch_missions
            },
        }

    def _mission_branches(self, missions: Sequence[Any]) -> dict[str, set[str]]:
        branches_by_identity = {
            item.identity_id: item.branch_id for item in self._organization.identities
        }
        result: dict[str, set[str]] = {}
        for mission in missions:
            if mission.current_crew_version is None:
                result[str(mission.mission_id)] = set()
                continue
            crew = self._missions.crew(str(mission.mission_id))
            result[str(mission.mission_id)] = {
                branches_by_identity[item.identity_id]
                for item in crew.members
                if item.identity_id in branches_by_identity
            }
        return result

    @staticmethod
    def _projection_metadata(limit: int) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "authoritative": False,
            "bounded": True,
            "limit": limit,
            "refresh_required_for_commands": True,
        }

    @staticmethod
    def _counts(values: Sequence[str] | Any) -> dict[str, int]:
        counts: dict[str, int] = {}
        for value in values:
            counts[value] = counts.get(value, 0) + 1
        return counts

    @classmethod
    def _evidence_references(cls, values: object) -> tuple[str, ...]:
        found: list[str] = []

        def walk(value: object, key: str = "") -> None:
            if hasattr(value, "model_dump"):
                walk(value.model_dump(mode="json"), key)
            elif isinstance(value, Mapping):
                for nested_key, nested in value.items():
                    walk(nested, str(nested_key))
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                for nested in value:
                    walk(nested, key)
            elif isinstance(value, str) and ("evidence" in key or key.endswith("references")):
                found.append(value)

        walk(values)
        return tuple(dict.fromkeys(found))

    @classmethod
    def _named_values(
        cls,
        values: object,
        *,
        names: set[str],
    ) -> tuple[object, ...]:
        found: list[object] = []

        def walk(value: object, key: str = "") -> None:
            if isinstance(value, Mapping):
                for nested_key, nested in value.items():
                    walk(nested, str(nested_key))
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                for nested in value:
                    walk(nested, key)
            elif key in names:
                found.append(value)

        walk(values)
        return tuple(found)

    @staticmethod
    def _require_limit(limit: int) -> None:
        if limit < 1 or limit > 1_000:
            raise MishkanError(ErrorCode.OUTPUT_CONTRACT, "inspection query bound is invalid")
