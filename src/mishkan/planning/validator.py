"""Deterministic acceptance of CrewAI-proposed repository plans."""

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.schema import SchemaRegistry
from mishkan.organization.models import OrganizationDefinition, OutcomeDefinition
from mishkan.planning.models import AcceptedPlan, PlanCandidate, PlannedToolCall
from mishkan.policy import ApprovalEvidence, AuthorizationRequest, Decision, EffectivePolicy
from mishkan.policy.evaluator import PolicyAuthority
from mishkan.repository.models import DiscoverySnapshot
from mishkan.tools.catalog import ToolCatalog
from mishkan.tools.gateway import (
    credential_references_for,
    declared_targets_for,
    policy_argument_values_for,
)
from mishkan.tools.gateway_models import DeclaredTargets
from mishkan.tools.inspection import ContentInspector
from mishkan.tools.models import RegistrySnapshot, ToolBinding, ToolContract


class PlanValidator:
    def __init__(
        self,
        catalog: ToolCatalog,
        policy: EffectivePolicy,
        authority: PolicyAuthority,
        inspector: ContentInspector | None = None,
        max_agent_iterations: int | None = None,
    ) -> None:
        self._catalog = catalog
        self._policy = policy
        self._authority = authority
        self._inspector = inspector
        self._max_agent_iterations = max_agent_iterations

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
        role_tools = {role.name: set(role.allowed_tools) for role in organization.roles}
        allowed_tools = set(outcome.allowed_tools)
        known_paths = {path.as_posix() for path in discovery.cited_paths}
        task_ids = [task.task_id for task in candidate.tasks]
        if len(task_ids) != len(set(task_ids)):
            violations.append("task identifiers must be unique")

        for task in candidate.tasks:
            if (
                self._max_agent_iterations is not None
                and len(task.tool_calls) + 1 > self._max_agent_iterations
            ):
                violations.append(
                    f"task {task.task_id} exact calls exceed the CrewAI iteration bound"
                )
            role_is_allowed = task.assigned_role in allowed_roles
            role_is_defined = task.assigned_role in organization_roles
            if not role_is_allowed or not role_is_defined:
                violations.append(f"task {task.task_id} uses an unauthorized role")
            unknown_tools = sorted(set(task.tools) - allowed_tools)
            if unknown_tools:
                violations.append(f"task {task.task_id} uses unauthorized tools: {unknown_tools}")
            role_forbidden_tools = sorted(
                set(task.tools) - role_tools.get(task.assigned_role, set())
            )
            if role_forbidden_tools:
                violations.append(
                    f"task {task.task_id} uses tools outside its role eligibility: "
                    f"{role_forbidden_tools}"
                )
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
            self._refuse(violations)

        registry = self._catalog.snapshot(
            tuple(dict.fromkeys(tool for task in candidate.tasks for tool in task.tools))
        )
        self._validate_planned_calls(candidate, registry, violations)
        if violations:
            self._refuse(violations)
        bindings = self._bindings(candidate, organization, outcome, registry)
        payload = candidate.model_dump(mode="json")
        payload["discovery_fingerprint"] = discovery.fingerprint
        payload["registry_fingerprint"] = registry.fingerprint
        payload["tool_bindings"] = [binding.model_dump(mode="json") for binding in bindings]
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        authorizations = []
        approval_requests: list[str] = []
        task_by_id = {task.task_id: task for task in candidate.tasks}
        for binding in bindings:
            contract = registry.require(binding.tool_id)
            source_task_id = (
                binding.task_id.removeprefix("review-")
                if binding.role in outcome.review_roles
                else binding.task_id
            )
            task = task_by_id[source_task_id]
            calls = tuple(call for call in task.tool_calls if call.tool_id == binding.tool_id)
            requests = (
                tuple(
                    self._authorization_request(
                        fingerprint,
                        discovery,
                        outcome,
                        binding,
                        contract,
                        call,
                    )
                    for call in calls
                )
                if calls
                else self._legacy_authorization_requests(
                    fingerprint,
                    discovery,
                    outcome,
                    binding,
                    contract,
                )
            )
            for request in requests:
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
            self._refuse(
                violations,
                approval_request_fingerprints=approval_requests,
            )
        accepted_payload = candidate.model_dump()
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

    @classmethod
    def _refuse(
        cls,
        violations: list[str],
        *,
        approval_request_fingerprints: list[str] | None = None,
    ) -> None:
        categories = cls._violation_categories(violations)
        details: dict[str, object] = {
            "violations": violations,
            "violation_categories": categories,
        }
        if approval_request_fingerprints:
            details["approval_request_fingerprints"] = approval_request_fingerprints
        raise MishkanError(
            ErrorCode.PLAN,
            "CrewAI plan candidate was refused: " + ", ".join(categories),
            details=details,
        )

    @staticmethod
    def _violation_categories(violations: list[str]) -> list[str]:
        matchers = (
            ("repository revision", "repository_revision"),
            ("outcome identifier", "outcome_identifier"),
            ("task count", "task_count"),
            ("task identifiers", "task_identifier"),
            ("iteration bound", "iteration_bound"),
            ("unauthorized role", "role_authority"),
            ("unauthorized tools", "tool_authority"),
            ("role eligibility", "role_tool_eligibility"),
            ("absent from discovery", "evidence_path"),
            ("unknown dependencies", "dependency"),
            ("depends on itself", "dependency"),
            ("dependencies contain a cycle", "dependency_cycle"),
            ("input schema", "tool_input_schema"),
            ("requires redaction", "content_inspection"),
            ("blocked content", "content_inspection"),
            ("binding ", "policy_authority"),
        )
        categories = [
            category
            for violation in violations
            for fragment, category in matchers
            if fragment in violation
        ]
        return list(dict.fromkeys(categories)) or ["plan_contract"]

    def _validate_planned_calls(
        self,
        candidate: PlanCandidate,
        registry: RegistrySnapshot,
        violations: list[str],
    ) -> None:
        for task in candidate.tasks:
            for call in task.tool_calls:
                contract = registry.require(call.tool_id)
                try:
                    Draft202012Validator.check_schema(contract.input_schema)
                    Draft202012Validator(contract.input_schema).validate(call.arguments)
                except (SchemaError, ValidationError) as exc:
                    violations.append(
                        f"task {task.task_id} call {call.call_id} fails {call.tool_id} input "
                        f"schema at {[str(item) for item in exc.path]}"
                    )
                    continue
                if self._inspector is not None:
                    serialized = json.dumps(
                        call.arguments,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    try:
                        if self._inspector.inspect(serialized) != serialized:
                            violations.append(
                                f"task {task.task_id} call {call.call_id} requires redaction"
                            )
                    except MishkanError:
                        violations.append(
                            f"task {task.task_id} call {call.call_id} contains blocked content"
                        )

    @staticmethod
    def _authorization_request(
        plan_fingerprint: str,
        discovery: DiscoverySnapshot,
        outcome: OutcomeDefinition,
        binding: ToolBinding,
        contract: ToolContract,
        call: PlannedToolCall,
    ) -> AuthorizationRequest:
        targets = declared_targets_for(contract, call.arguments)
        return AuthorizationRequest(
            plan_fingerprint=plan_fingerprint,
            identity=f"role:{binding.role}",
            objective_class=outcome.objective_class,
            repository=discovery.binding.repository_id,
            outcome=outcome.outcome_id,
            role=binding.role,
            capability=binding.tool_id,
            effect_class=contract.effect_class.value,
            paths=targets.paths,
            executables=targets.executables,
            arguments=policy_argument_values_for(contract, call.arguments),
            network_destinations=targets.network_destinations,
            remotes=targets.remotes,
            branches=targets.branches,
            environments=targets.environments,
            credentials=credential_references_for(contract, call.arguments),
            external_resources=targets.external_resources,
            resources=contract.resources,
        )

    @staticmethod
    def _legacy_authorization_requests(
        plan_fingerprint: str,
        discovery: DiscoverySnapshot,
        outcome: OutcomeDefinition,
        binding: ToolBinding,
        contract: ToolContract,
    ) -> tuple[AuthorizationRequest, ...]:
        path_requests = (
            tuple((target,) for target in binding.allowed_targets)
            if "path" in contract.target_scopes
            else ((),)
        )
        return tuple(
            AuthorizationRequest(
                plan_fingerprint=plan_fingerprint,
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
            for paths in path_requests
        )

    def _bindings(
        self,
        candidate: PlanCandidate,
        organization: OrganizationDefinition,
        outcome: OutcomeDefinition,
        registry: RegistrySnapshot,
    ) -> tuple[ToolBinding, ...]:
        bindings: list[ToolBinding] = []
        role_tools = {role.name: set(role.allowed_tools) for role in organization.roles}
        for task in candidate.tasks:
            for tool_id in task.tools:
                calls = tuple(call for call in task.tool_calls if call.tool_id == tool_id)
                targets = self._binding_targets(calls, registry.require(tool_id))
                bindings.append(
                    self._catalog.bind(
                        registry,
                        task.task_id,
                        task.assigned_role,
                        tool_id,
                        targets or task.evidence_paths,
                        tuple(call.argument_fingerprint for call in calls),
                    )
                )
            for role in outcome.review_roles:
                for tool_id in task.tools:
                    contract = registry.require(tool_id)
                    if contract.effect_class.value != "read" or tool_id not in role_tools[role]:
                        continue
                    calls = tuple(call for call in task.tool_calls if call.tool_id == tool_id)
                    targets = self._binding_targets(calls, contract)
                    bindings.append(
                        self._catalog.bind(
                            registry,
                            f"review-{task.task_id}",
                            role,
                            tool_id,
                            targets or task.evidence_paths,
                            tuple(call.argument_fingerprint for call in calls),
                        )
                    )
        return tuple(bindings)

    @staticmethod
    def _binding_targets(
        calls: tuple[PlannedToolCall, ...],
        contract: ToolContract,
    ) -> tuple[str, ...]:
        values: list[str] = []
        for call in calls:
            targets: DeclaredTargets = declared_targets_for(contract, call.arguments)
            values.extend(targets.paths)
            values.extend(targets.executables)
            values.extend(targets.network_destinations)
            values.extend(targets.repositories)
            values.extend(targets.remotes)
            values.extend(targets.branches)
            values.extend(targets.environments)
            values.extend(targets.external_resources)
        return tuple(dict.fromkeys(values))

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
