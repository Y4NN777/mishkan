"""Deterministic acceptance of CrewAI-proposed repository plans."""

import hashlib
import json
from pathlib import Path

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.schema import SchemaRegistry
from mishkan.organization.models import OrganizationDefinition, OutcomeDefinition
from mishkan.planning.models import AcceptedPlan, PlanCandidate
from mishkan.repository.models import DiscoverySnapshot


class PlanValidator:
    def accept(
        self,
        candidate: PlanCandidate,
        discovery: DiscoverySnapshot,
        organization: OrganizationDefinition,
        outcome: OutcomeDefinition,
    ) -> AcceptedPlan:
        SchemaRegistry.require_supported("mishkan.plan", candidate.schema_version)
        violations: list[str] = []
        if candidate.repository_revision != discovery.binding.base_revision:
            violations.append("repository revision does not match discovery")
        if candidate.outcome_id != outcome.outcome_id:
            violations.append("outcome identifier does not match requested outcome")
        if len(candidate.tasks) > outcome.max_tasks:
            violations.append("task count exceeds outcome limit")

        allowed_roles = set(outcome.task_roles)
        organization_roles = {role.name for role in organization.roles}
        allowed_tools = set(outcome.allowed_tools)
        known_paths = {path.as_posix() for path in discovery.cited_paths}
        task_ids = [task.task_id for task in candidate.tasks]
        if len(task_ids) != len(set(task_ids)):
            violations.append("task identifiers must be unique")

        for task in candidate.tasks:
            role_is_allowed = task.assigned_role in allowed_roles
            role_is_defined = task.assigned_role in organization_roles
            if not role_is_allowed or not role_is_defined:
                violations.append(f"task {task.task_id} uses an unauthorized role")
            unknown_tools = sorted(set(task.tools) - allowed_tools)
            if unknown_tools:
                violations.append(f"task {task.task_id} uses unauthorized tools: {unknown_tools}")
            invalid_paths = sorted(
                path for path in task.evidence_paths if Path(path).as_posix() not in known_paths
            )
            if invalid_paths:
                violations.append(
                    f"task {task.task_id} cites paths absent from discovery: {invalid_paths}"
                )
            unknown_dependencies = sorted(set(task.depends_on) - set(task_ids))
            if unknown_dependencies:
                violations.append(
                    f"task {task.task_id} has unknown dependencies: {unknown_dependencies}"
                )
            if task.task_id in task.depends_on:
                violations.append(f"task {task.task_id} depends on itself")

        if self._has_cycle(candidate):
            violations.append("task dependencies contain a cycle")
        if violations:
            raise MishkanError(
                ErrorCode.PLAN,
                "CrewAI plan candidate was refused",
                details={"violations": violations},
            )

        payload = candidate.model_dump(mode="json")
        payload["discovery_fingerprint"] = discovery.fingerprint
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return AcceptedPlan(
            **candidate.model_dump(),
            fingerprint=fingerprint,
            discovery_fingerprint=discovery.fingerprint,
        )

    @staticmethod
    def _has_cycle(candidate: PlanCandidate) -> bool:
        dependencies = {task.task_id: set(task.depends_on) for task in candidate.tasks}
        remaining = set(dependencies)
        while remaining:
            ready = {task_id for task_id in remaining if not dependencies[task_id] & remaining}
            if not ready:
                return True
            remaining -= ready
        return False
