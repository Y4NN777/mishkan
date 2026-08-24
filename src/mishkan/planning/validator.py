"""Deterministic acceptance of CrewAI-proposed repository plans."""

import hashlib
import json
from pathlib import Path

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.schema import SchemaRegistry
from mishkan.organization.models import OrganizationDefinition, OutcomeDefinition
from mishkan.planning.models import AcceptedPlan, PlanCandidate
from mishkan.policy import ApprovalEvidence, AuthorizationRequest, Decision, EffectivePolicy
from mishkan.policy.evaluator import PolicyAuthority
from mishkan.repository.models import DiscoverySnapshot
from mishkan.tools.catalog import ToolCatalog
from mishkan.tools.models import RegistrySnapshot, ToolBinding


class PlanValidator:
    def __init__(
        self,
        catalog: ToolCatalog,
        policy: EffectivePolicy,
        authority: PolicyAuthority,
    ) -> None:
        self._catalog = catalog
        self._policy = policy
        self._authority = authority

    def accept(
        self,
        candidate: PlanCandidate,
        discovery: DiscoverySnapshot,
        organization: OrganizationDefinition,
        outcome: OutcomeDefinition,
        approvals: tuple[ApprovalEvidence, ...] = (),
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

        registry = self._catalog.snapshot(
            tuple(dict.fromkeys(tool for task in candidate.tasks for tool in task.tools))
        )
        bindings = self._bindings(candidate, outcome, registry)
        payload = candidate.model_dump(mode="json")
        payload["discovery_fingerprint"] = discovery.fingerprint
        payload["registry_fingerprint"] = registry.fingerprint
        payload["tool_bindings"] = [binding.model_dump(mode="json") for binding in bindings]
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        authorizations = []
        approval_requests: list[str] = []
        for binding in bindings:
            contract = registry.require(binding.tool_id)
            path_requests = (
                tuple((target,) for target in binding.allowed_targets)
                if "path" in contract.target_scopes
                else ((),)
            )
            for paths in path_requests:
                request = AuthorizationRequest(
                    plan_fingerprint=fingerprint,
                    identity=f"role:{binding.role}",
                    objective_class=outcome.objective_class,
                    repository=discovery.binding.repository_id,
                    outcome=outcome.outcome_id,
                    role=binding.role,
                    capability=binding.tool_id,
                    effect_class=contract.effect_class.value,
                    paths=paths,
                    credentials=contract.credential_refs,
                    resources=contract.resources,
                )
                approval = next(
                    (
                        evidence
                        for evidence in approvals
                        if evidence.request_fingerprint == request.fingerprint
                    ),
                    None,
                )
                decision = self._authority.evaluate(request, self._policy, approval)
                if decision.decision is not Decision.ALLOW:
                    violations.append(
                        f"binding {binding.task_id}/{binding.tool_id} is {decision.decision.value}"
                    )
                    if decision.decision is Decision.REQUIRE_APPROVAL:
                        approval_requests.append(request.fingerprint)
                authorizations.append(decision)
        if violations:
            raise MishkanError(
                ErrorCode.PLAN,
                "CrewAI plan candidate was refused",
                details={
                    "violations": violations,
                    "approval_request_fingerprints": approval_requests,
                },
            )
        accepted_payload = candidate.model_dump()
        accepted_payload["schema_version"] = "1.1"
        return AcceptedPlan(
            **accepted_payload,
            fingerprint=fingerprint,
            discovery_fingerprint=discovery.fingerprint,
            registry=registry,
            tool_bindings=bindings,
            policy_fingerprint=self._policy.fingerprint,
            approvals=approvals,
            authorizations=tuple(authorizations),
        )

    def _bindings(
        self,
        candidate: PlanCandidate,
        outcome: OutcomeDefinition,
        registry: RegistrySnapshot,
    ) -> tuple[ToolBinding, ...]:
        bindings: list[ToolBinding] = []
        for task in candidate.tasks:
            for tool_id in task.tools:
                bindings.append(
                    self._catalog.bind(
                        registry,
                        task.task_id,
                        task.assigned_role,
                        tool_id,
                        task.evidence_paths,
                    )
                )
            for role in outcome.review_roles:
                for tool_id in task.tools:
                    bindings.append(
                        self._catalog.bind(
                            registry,
                            f"review-{task.task_id}",
                            role,
                            tool_id,
                            task.evidence_paths,
                        )
                    )
        return tuple(bindings)

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
