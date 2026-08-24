"""Fixed deterministic enforcement pipeline for every capability invocation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.policy import ApprovalEvidence, AuthorizationRequest, Decision, PolicyAuthority
from mishkan.policy.models import security_identifier
from mishkan.tools.adapters import AdapterCall, CapabilityAdapter
from mishkan.tools.gateway_models import (
    AuditEvent,
    CallStatus,
    DeclaredTargets,
    InvocationContext,
    InvocationEnvelope,
    ResolvedPath,
    ResolvedTargets,
    ToolResultEnvelope,
)
from mishkan.tools.inspection import ContentInspector


class CredentialResolver(Protocol):
    def resolve(self, references: tuple[str, ...]) -> dict[str, str]: ...


class EvidenceSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...


class MemoryEvidenceSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


class MappingCredentialResolver:
    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)
        self.calls = 0

    def resolve(self, references: tuple[str, ...]) -> dict[str, str]:
        self.calls += 1
        missing = [reference for reference in references if reference not in self._values]
        if missing:
            raise MishkanError(
                ErrorCode.TOOL_UNAVAILABLE,
                "required credential references are unavailable",
                details={"references": missing},
            )
        return {reference: self._values[reference] for reference in references}


class CapabilityGateway:
    """Integrity-stage order is fixed; every operational decision comes from passed contracts."""

    def __init__(
        self,
        repository_root: Path,
        policy_authority: PolicyAuthority,
        credential_resolver: CredentialResolver,
        inspector: ContentInspector,
        adapters: Mapping[str, CapabilityAdapter],
        evidence: EvidenceSink,
    ) -> None:
        self._root = Path(repository_root).resolve()
        self._policy = policy_authority
        self._credentials = credential_resolver
        self._inspector = inspector
        self._adapters = dict(adapters)
        self._evidence = evidence

    def invoke(
        self,
        context: InvocationContext,
        arguments: dict[str, Any],
        declared_targets: DeclaredTargets,
        approval: ApprovalEvidence | None = None,
    ) -> ToolResultEnvelope:
        started = utc_now()
        call_id: str | None = None
        contract = context.registry.require(context.binding.tool_id)
        try:
            self._validate_binding(context, contract.provenance_fingerprint)
            self._validate_schema(contract.input_schema, arguments, "input")
            resolved = self._resolve_targets(declared_targets)
            self._validate_declared_arguments(contract.target_scopes, arguments, resolved)
            self._validate_bound_targets(context.binding.allowed_targets, resolved)
            request = AuthorizationRequest(
                plan_fingerprint=context.plan_fingerprint,
                identity=context.identity,
                objective_class=context.objective_class,
                repository=context.repository,
                outcome=context.outcome,
                role=context.role,
                capability=contract.tool_id,
                effect_class=contract.effect_class.value,
                paths=tuple(path.relative for path in resolved.paths),
                executables=resolved.executables,
                network_destinations=resolved.network_destinations,
                remotes=resolved.remotes,
                branches=resolved.branches,
                environments=resolved.environments,
                credentials=contract.credential_refs,
                external_resources=resolved.external_resources,
                isolation_profile=context.isolation_profile,
                resources=context.resources,
            )
            authorization = self._policy.evaluate(request, context.policy, approval)
            if authorization.decision is Decision.REQUIRE_APPROVAL:
                raise MishkanError(
                    ErrorCode.AUTHORIZATION_MISSING,
                    "capability requires matching interactive approval",
                    details={"request_fingerprint": request.fingerprint},
                )
            if authorization.decision is Decision.DENY:
                raise MishkanError(
                    ErrorCode.AUTHORITY_NOT_GRANTED,
                    "effective policy denies the exact capability request",
                    details={"request_fingerprint": request.fingerprint},
                )
            deadline = started + timedelta(seconds=context.resources.timeout_seconds)
            envelope = InvocationEnvelope(
                run_id=context.run_id,
                task_attempt_id=context.task_attempt_id,
                acting_identity=context.identity,
                tool_id=contract.tool_id,
                tool_version=contract.version,
                registry_fingerprint=context.registry.fingerprint,
                plan_fingerprint=context.plan_fingerprint,
                policy_fingerprint=context.policy.fingerprint,
                normalized_arguments=arguments,
                declared_targets=declared_targets,
                authorization=authorization,
                deadline=deadline,
            )
            call_id = str(envelope.id)
            self._audit(context, call_id, "tool.call_authorized", "allow", authorization.reason)
            credentials = self._credentials.resolve(contract.credential_refs)
            adapter = self._adapters.get(contract.adapter)
            if adapter is None:
                raise MishkanError(
                    ErrorCode.TOOL_UNAVAILABLE,
                    "bound tool adapter is unavailable",
                    details={"adapter": contract.adapter},
                )
            adapter_result = adapter.invoke(AdapterCall(arguments, resolved, credentials))
            secret_values = tuple(credentials.values())
            inspected = self._inspector.inspect(
                json.dumps(adapter_result.output, sort_keys=True, default=str), secret_values
            )
            inspected_output = json.loads(inspected)
            if not isinstance(inspected_output, dict):
                raise MishkanError(ErrorCode.TOOL_SCHEMA, "inspected tool output is not an object")
            self._validate_schema(contract.result_schema, inspected_output, "result")
            completed = ToolResultEnvelope(
                call_id=call_id,
                run_id=context.run_id,
                task_attempt_id=context.task_attempt_id,
                tool_id=contract.tool_id,
                tool_version=contract.version,
                status=CallStatus.COMPLETED,
                started_at=started,
                completed_at=utc_now(),
                output=inspected_output,
                actual_targets=adapter_result.actual_targets,
                external_references=adapter_result.external_references,
                retryable=False,
                adapter_evidence=adapter_result.evidence,
                reason="validated authorized tool result",
            )
            self._audit(context, call_id, "tool.call_completed", "allow", completed.reason)
            return completed
        except TimeoutError:
            terminal_result = self._terminal(
                context,
                call_id,
                started,
                CallStatus.UNCERTAIN,
                ErrorCode.TOOL_EFFECT,
                "tool timeout left effect state uncertain",
            )
            self._audit(
                context,
                terminal_result.call_id,
                "tool.call_uncertain",
                "uncertain",
                terminal_result.reason,
            )
            return terminal_result
        except (MishkanError, OSError, ValueError) as exc:
            if isinstance(exc, MishkanError):
                code = exc.envelope.code
                reason = exc.envelope.message
            else:
                code = ErrorCode.TOOL_EFFECT
                reason = f"tool effect failed: {type(exc).__name__}"
            pre_dispatch_codes = {
                ErrorCode.AUTHORIZATION_MISSING,
                ErrorCode.AUTHORITY_NOT_GRANTED,
                ErrorCode.POLICY_CONFLICT,
                ErrorCode.TOOL_UNAVAILABLE,
                ErrorCode.TOOL_DRIFT,
            }
            if code is ErrorCode.TOOL_SCHEMA and call_id is None:
                pre_dispatch_codes.add(ErrorCode.TOOL_SCHEMA)
            status = CallStatus.REFUSED if code in pre_dispatch_codes else CallStatus.FAILED
            terminal_result = self._terminal(context, call_id, started, status, code, reason)
            self._audit(context, terminal_result.call_id, "tool.call_refused", status.value, reason)
            return terminal_result

    def _resolve_targets(self, declared: DeclaredTargets) -> ResolvedTargets:
        paths: list[ResolvedPath] = []
        for value in declared.paths:
            try:
                security_identifier(value)
            except ValueError as exc:
                raise MishkanError(
                    ErrorCode.TOOL_SCHEMA,
                    "filesystem target contains unstable Unicode",
                    details={"path": value},
                ) from exc
            candidate = self._root / value
            resolved = candidate.resolve(strict=False)
            if candidate.is_absolute() and not resolved.is_relative_to(self._root):
                raise MishkanError(
                    ErrorCode.AUTHORITY_NOT_GRANTED,
                    "filesystem target resolves outside the accepted workspace",
                    details={"path": value},
                )
            if not resolved.is_relative_to(self._root):
                raise MishkanError(
                    ErrorCode.AUTHORITY_NOT_GRANTED,
                    "filesystem target resolves outside the accepted workspace",
                    details={"path": value},
                )
            paths.append(
                ResolvedPath(
                    requested=value,
                    relative=resolved.relative_to(self._root).as_posix(),
                    absolute=resolved,
                )
            )
        collections = {
            "executables": declared.executables,
            "network_destinations": declared.network_destinations,
            "repositories": declared.repositories,
            "remotes": declared.remotes,
            "branches": declared.branches,
            "environments": declared.environments,
            "external_resources": declared.external_resources,
        }
        try:
            normalized = {
                key: tuple(security_identifier(value) for value in values)
                for key, values in collections.items()
            }
        except ValueError as exc:
            raise MishkanError(
                ErrorCode.TOOL_SCHEMA,
                "declared target contains unstable Unicode",
            ) from exc
        return ResolvedTargets(paths=tuple(paths), **normalized)

    @staticmethod
    def _validate_binding(context: InvocationContext, contract_fingerprint: str) -> None:
        binding = context.binding
        if binding.registry_fingerprint != context.registry.fingerprint:
            raise MishkanError(ErrorCode.TOOL_DRIFT, "task binding registry snapshot drifted")
        if binding.contract_fingerprint != contract_fingerprint:
            raise MishkanError(ErrorCode.TOOL_DRIFT, "task binding tool contract drifted")
        if (
            binding.task_id != context.task_attempt_id.split(":", 1)[0]
            or binding.role != context.role
        ):
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "tool binding does not belong to the acting task and role",
            )

    @staticmethod
    def _validate_schema(schema: dict[str, Any], value: dict[str, Any], boundary: str) -> None:
        try:
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(value)
        except (SchemaError, ValidationError) as exc:
            raise MishkanError(
                ErrorCode.TOOL_SCHEMA,
                f"tool {boundary} failed its bound JSON Schema",
                details={"boundary": boundary, "path": [str(item) for item in exc.path]},
            ) from exc

    @staticmethod
    def _validate_declared_arguments(
        target_scopes: tuple[str, ...],
        arguments: dict[str, Any],
        targets: ResolvedTargets,
    ) -> None:
        declared: dict[str, tuple[str, ...]] = {
            "path": tuple(path.requested for path in targets.paths),
            "executable": targets.executables,
            "network": targets.network_destinations,
            "repository": targets.repositories,
            "remote": targets.remotes,
            "branch": targets.branches,
            "environment": targets.environments,
            "external_resource": targets.external_resources,
        }
        unsupported = [
            scope for scope, values in declared.items() if values and scope not in target_scopes
        ]
        if unsupported:
            raise MishkanError(
                ErrorCode.TOOL_SCHEMA,
                "tool invocation declared unsupported target scopes",
                details={"scopes": unsupported},
            )
        extracted = CapabilityGateway._argument_targets(arguments)
        mismatched = [
            value
            for scope in target_scopes
            for value in declared[scope]
            if value not in extracted[scope]
        ]
        if mismatched:
            raise MishkanError(
                ErrorCode.TOOL_SCHEMA,
                "declared targets do not match tool arguments",
                details={"count": len(mismatched)},
            )

    @staticmethod
    def _argument_targets(arguments: dict[str, Any]) -> dict[str, tuple[str, ...]]:
        def strings(*keys: str) -> tuple[str, ...]:
            values: list[str] = []
            for key in keys:
                value = arguments.get(key)
                if isinstance(value, str):
                    values.append(value)
                elif isinstance(value, list):
                    values.extend(item for item in value if isinstance(item, str))
            return tuple(values)

        argv = arguments.get("argv")
        executables = (
            (argv[0],) if isinstance(argv, list) and argv and isinstance(argv[0], str) else ()
        )
        return {
            "path": strings("path", "paths", "workspace"),
            "executable": executables,
            "network": strings("network_destination", "network_destinations", "destination"),
            "repository": strings("repository"),
            "remote": strings("remote"),
            "branch": strings("branch", "local_branch", "remote_branch"),
            "environment": strings("environment"),
            "external_resource": strings("external_resource", "target", "artifact"),
        }

    @staticmethod
    def _validate_bound_targets(allowed: tuple[str, ...], targets: ResolvedTargets) -> None:
        actual = (
            *(path.relative for path in targets.paths),
            *targets.executables,
            *targets.network_destinations,
            *targets.repositories,
            *targets.remotes,
            *targets.branches,
            *targets.environments,
            *targets.external_resources,
        )
        refused = [
            value for value in actual if not any(fnmatchcase(value, item) for item in allowed)
        ]
        if refused:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "resolved target is outside the exact task binding",
                details={"targets": refused},
            )

    def _terminal(
        self,
        context: InvocationContext,
        call_id: str | None,
        started: Any,
        status: CallStatus,
        code: ErrorCode,
        reason: str,
    ) -> ToolResultEnvelope:
        return ToolResultEnvelope(
            call_id=call_id or "unissued",
            run_id=context.run_id,
            task_attempt_id=context.task_attempt_id,
            tool_id=context.binding.tool_id,
            tool_version=context.binding.tool_version,
            status=status,
            started_at=started,
            completed_at=utc_now(),
            retryable=False,
            error_code=code.value,
            reason=reason,
        )

    def _audit(
        self,
        context: InvocationContext,
        call_id: str,
        event_type: str,
        decision: str,
        reason: str,
    ) -> None:
        inspected_reason = self._inspector.inspect(reason)
        self._evidence.record(
            AuditEvent(
                event_type=event_type,
                run_id=context.run_id,
                task_attempt_id=context.task_attempt_id,
                call_id=call_id,
                identity=context.identity,
                capability=context.binding.tool_id,
                decision=decision,
                reason=inspected_reason,
            )
        )
