"""Fixed deterministic enforcement pipeline for every capability invocation."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from datetime import timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]

from mishkan.artifacts import ArtifactProvenance, ArtifactStore
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.policy import ApprovalEvidence, AuthorizationRequest, Decision, PolicyAuthority
from mishkan.policy.models import security_identifier
from mishkan.tools.adapters import AdapterCall, CapabilityAdapter
from mishkan.tools.execution import EffectSettlement
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
from mishkan.tools.models import ToolContract, argument_fingerprint

_TARGET_FIELDS = {
    "path": "paths",
    "executable": "executables",
    "network": "network_destinations",
    "repository": "repositories",
    "remote": "remotes",
    "branch": "branches",
    "environment": "environments",
    "external_resource": "external_resources",
}


def declared_targets_for(
    contract: ToolContract,
    arguments: dict[str, Any],
) -> DeclaredTargets:
    """Extract targets only through the selectors declared by the versioned tool contract."""
    values: dict[str, tuple[str, ...]] = {}
    for scope, selectors in contract.target_arguments.items():
        field = _TARGET_FIELDS.get(scope)
        if field is None:
            raise MishkanError(
                ErrorCode.TOOL_CONTRACT,
                "tool contract declares an unsupported target scope",
                details={"scope": scope},
            )
        extracted = tuple(
            dict.fromkeys(
                value
                for selector in selectors
                for value in _select_argument_values(arguments, selector)
            )
        )
        values[field] = extracted
    return DeclaredTargets(**values)


def _select_argument_values(arguments: dict[str, Any], selector: str) -> tuple[str, ...]:
    current = _select_argument_nodes(arguments, selector)
    flattened: list[str] = []
    for value in current:
        if isinstance(value, str):
            flattened.append(value)
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            flattened.extend(value)
    return tuple(flattened)


def _select_argument_nodes(arguments: dict[str, Any], selector: str) -> tuple[Any, ...]:
    current: list[Any] = [arguments]
    for part in selector.split("."):
        selected: list[Any] = []
        for value in current:
            if isinstance(value, dict) and part == "@keys":
                selected.extend(value)
            elif isinstance(value, dict) and part == "*":
                selected.extend(value.values())
            elif isinstance(value, list) and part == "*":
                selected.extend(value)
            elif isinstance(value, dict) and part in value:
                selected.append(value[part])
            elif isinstance(value, list) and part.isdigit():
                index = int(part)
                if index < len(value):
                    selected.append(value[index])
        current = selected
        if not current:
            return ()
    return tuple(current)


def credential_references_for(
    contract: ToolContract,
    arguments: dict[str, Any],
) -> tuple[str, ...]:
    dynamic = (
        reference
        for selector in contract.credential_arguments
        for reference in _select_argument_values(arguments, selector)
    )
    return tuple(dict.fromkeys((*contract.credential_refs, *dynamic)))


def policy_argument_values_for(
    contract: ToolContract,
    arguments: dict[str, Any],
) -> tuple[str, ...]:
    return tuple(
        value
        for selector in contract.policy_arguments
        for value in _select_argument_values(arguments, selector)
    )


class CredentialResolver(Protocol):
    def resolve(self, references: tuple[str, ...]) -> dict[str, str]: ...


class EvidenceSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...


class PlannedCallJournal(Protocol):
    def reserve_planned_call(
        self,
        *,
        invocation_id: str,
        run_id: str,
        task_attempt_id: str,
        planned_call_id: str,
        request_fingerprint: str,
        tool_id: str,
        tool_version: str,
        effect_class: str,
        declared_effects: tuple[str, ...],
    ) -> ToolResultEnvelope | None: ...

    def mark_planned_call_dispatching(
        self,
        invocation_id: str,
        request_fingerprint: str,
    ) -> None: ...

    def complete_planned_call(
        self,
        invocation_id: str,
        request_fingerprint: str,
        result: ToolResultEnvelope,
        effect_settlement: EffectSettlement,
    ) -> ToolResultEnvelope: ...


class CancellationSignal(Protocol):
    def requested(self, run_id: str, task_attempt_id: str) -> bool: ...


class NeverCancelled:
    def requested(self, run_id: str, task_attempt_id: str) -> bool:
        del run_id, task_attempt_id
        return False


class CapabilityCancelled(Exception):
    """Raised by an adapter when an observed cancellation stops its work."""


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
        cancellation: CancellationSignal | None = None,
        artifact_store: ArtifactStore | None = None,
        planned_calls: PlannedCallJournal | None = None,
    ) -> None:
        self._root = Path(repository_root).resolve()
        self._policy = policy_authority
        self._credentials = credential_resolver
        self._inspector = inspector
        self._adapters = dict(adapters)
        self._evidence = evidence
        self._cancellation = cancellation or NeverCancelled()
        self._artifact_store = artifact_store
        self._planned_calls = planned_calls

    def invoke(
        self,
        context: InvocationContext,
        arguments: dict[str, Any],
        declared_targets: DeclaredTargets,
        approval: ApprovalEvidence | tuple[ApprovalEvidence, ...] | None = None,
        planned_call_id: str | None = None,
    ) -> ToolResultEnvelope:
        started = utc_now()
        call_id: str | None = None
        request_fingerprint: str | None = None
        journal_reserved = False
        dispatch_started = False
        dispatched_status: CallStatus | None = None
        contract = context.registry.require(context.binding.tool_id)
        try:
            self._validate_binding(context, contract.provenance_fingerprint)
            self._validate_schema(contract.input_schema, arguments, "input")
            serialized_arguments = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
            self._validate_bound_call(context.binding.allowed_call_fingerprints, arguments)
            if self._inspector.inspect(serialized_arguments) != serialized_arguments:
                raise MishkanError(
                    ErrorCode.SECRET_CONTENT,
                    "tool arguments require redaction and cannot be executed faithfully",
                )
            credential_references = credential_references_for(contract, arguments)
            policy_arguments = policy_argument_values_for(contract, arguments)
            resolved = self._resolve_targets(declared_targets)
            self._validate_declared_arguments(contract, arguments, resolved)
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
                effects=_select_argument_values(arguments, "declared_effects"),
                paths=tuple(path.relative for path in resolved.paths),
                executables=resolved.executables,
                arguments=policy_arguments,
                network_destinations=resolved.network_destinations,
                remotes=resolved.remotes,
                branches=resolved.branches,
                environments=resolved.environments,
                credentials=credential_references,
                external_resources=resolved.external_resources,
                isolation_profile=context.isolation_profile,
                resources=context.resources,
            )
            approval_candidates = approval if isinstance(approval, tuple) else (approval,)
            exact_approval = next(
                (
                    evidence
                    for evidence in approval_candidates
                    if evidence is not None and evidence.request_fingerprint == request.fingerprint
                ),
                None,
            )
            authorization = self._policy.evaluate(request, context.policy, exact_approval)
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
            if contract.artifact_output_argument is not None:
                artifact_values = _select_argument_nodes(
                    arguments, contract.artifact_output_argument
                )
                if artifact_values == (True,) and self._artifact_store is None:
                    raise MishkanError(
                        ErrorCode.TOOL_UNAVAILABLE,
                        "requested complete-output artifacts require a configured artifact store",
                        details={"tool_id": contract.tool_id},
                    )
            deadline = started + timedelta(seconds=context.resources.timeout_seconds)
            invocation_id = (
                uuid5(
                    NAMESPACE_URL,
                    ":".join(
                        (
                            "mishkan",
                            context.run_id,
                            context.task_attempt_id,
                            planned_call_id,
                        )
                    ),
                )
                if planned_call_id is not None
                else uuid4()
            )
            envelope = InvocationEnvelope(
                id=invocation_id,
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
            if planned_call_id is not None and self._planned_calls is not None:
                request_fingerprint = self._planned_request_fingerprint(
                    context,
                    contract,
                    planned_call_id,
                    arguments,
                    declared_targets,
                    authorization.request_fingerprint,
                )
                replayed = self._planned_calls.reserve_planned_call(
                    invocation_id=call_id,
                    run_id=context.run_id,
                    task_attempt_id=context.task_attempt_id,
                    planned_call_id=planned_call_id,
                    request_fingerprint=request_fingerprint,
                    tool_id=contract.tool_id,
                    tool_version=contract.version,
                    effect_class=contract.effect_class.value,
                    declared_effects=request.effects,
                )
                if replayed is not None:
                    self._audit(
                        context,
                        call_id,
                        "tool.call_replayed",
                        replayed.status.value,
                        "returned the exact durable planned-call result",
                        {"planned_call_id": planned_call_id},
                    )
                    return replayed
                journal_reserved = True
            self._audit(
                context,
                call_id,
                "tool.call_authorized",
                "allow",
                authorization.reason,
                {
                    "authorization_id": str(authorization.id),
                    "request_fingerprint": authorization.request_fingerprint,
                    "approval_id": authorization.approval_id,
                    "matched_rule_ids": authorization.matched_rule_ids,
                },
            )
            if self._cancellation.requested(context.run_id, context.task_attempt_id):
                raise CapabilityCancelled("cancellation requested before dispatch")
            credentials = self._credentials.resolve(credential_references)
            adapter = self._adapters.get(contract.adapter)
            if adapter is None:
                raise MishkanError(
                    ErrorCode.TOOL_UNAVAILABLE,
                    "bound tool adapter is unavailable",
                    details={"adapter": contract.adapter},
                )
            if journal_reserved:
                assert request_fingerprint is not None
                self._planned_calls_or_raise().mark_planned_call_dispatching(
                    call_id,
                    request_fingerprint,
                )
                dispatch_started = True
            adapter_result = adapter.invoke(
                AdapterCall(
                    arguments=arguments,
                    targets=resolved,
                    credentials=credentials,
                    execution_id=call_id,
                    resources=context.resources,
                    isolation_profile=context.isolation_profile,
                    cancellation_requested=lambda: self._cancellation.requested(
                        context.run_id, context.task_attempt_id
                    ),
                    run_id=context.run_id,
                    task_attempt_id=context.task_attempt_id,
                    acting_identity=context.identity,
                    capability=contract.tool_id,
                    plan_fingerprint=context.plan_fingerprint,
                    objective_class=context.objective_class,
                    repository=context.repository,
                    outcome=context.outcome,
                    role=context.role,
                )
            )
            dispatched_status = adapter_result.call_status
            if adapter_result.actual_targets != resolved:
                raise MishkanError(
                    ErrorCode.TOOL_EFFECT,
                    "adapter-reported actual targets differ from the authorized targets",
                )
            secret_values = tuple(credentials.values())
            for content in adapter_result.inspection_content:
                self._inspector.inspect(content, secret_values)
            output_with_artifacts = dict(adapter_result.output)
            references_with_artifacts = list(adapter_result.external_references)
            artifact_channels: set[str] = set()
            for candidate in adapter_result.artifact_candidates:
                if candidate.channel in artifact_channels:
                    raise MishkanError(
                        ErrorCode.TOOL_SCHEMA,
                        "adapter returned duplicate artifact output channels",
                    )
                artifact_channels.add(candidate.channel)
                self._inspector.inspect(
                    candidate.content.decode("utf-8", errors="replace"), secret_values
                )
                if self._artifact_store is None:
                    raise MishkanError(
                        ErrorCode.ARTIFACT,
                        "adapter returned artifact content without a configured store",
                    )
                manifest = self._artifact_store.put_bytes(
                    candidate.content,
                    media_type=candidate.media_type,
                    provenance=ArtifactProvenance(
                        producer_identity=context.identity,
                        run_id=context.run_id,
                        task_attempt_id=context.task_attempt_id,
                        call_id=call_id,
                        capability=contract.tool_id,
                        channel=candidate.channel,
                    ),
                    complete=candidate.complete,
                )
                reference = manifest.reference
                output_with_artifacts[f"{candidate.channel}_artifact_ref"] = reference
                references_with_artifacts.append(reference)
            inspected = self._inspector.inspect(
                json.dumps(
                    {
                        "output": output_with_artifacts,
                        "external_references": references_with_artifacts,
                        "evidence": adapter_result.evidence,
                    },
                    sort_keys=True,
                    default=str,
                ),
                secret_values,
            )
            inspected_result = json.loads(inspected)
            if not isinstance(inspected_result, dict):
                raise MishkanError(ErrorCode.TOOL_SCHEMA, "inspected tool result is not an object")
            inspected_output = inspected_result.get("output")
            if not isinstance(inspected_output, dict):
                raise MishkanError(ErrorCode.TOOL_SCHEMA, "inspected tool output is not an object")
            external_references = inspected_result.get("external_references")
            adapter_evidence = inspected_result.get("evidence")
            if not isinstance(external_references, list) or not all(
                isinstance(item, str) for item in external_references
            ):
                raise MishkanError(
                    ErrorCode.TOOL_SCHEMA,
                    "inspected external references are not a string list",
                )
            if not isinstance(adapter_evidence, dict):
                raise MishkanError(ErrorCode.TOOL_SCHEMA, "inspected adapter evidence is invalid")
            self._validate_schema(contract.result_schema, inspected_output, "result")
            completed = ToolResultEnvelope(
                call_id=call_id,
                run_id=context.run_id,
                task_attempt_id=context.task_attempt_id,
                tool_id=contract.tool_id,
                tool_version=contract.version,
                status=adapter_result.call_status,
                started_at=started,
                completed_at=utc_now(),
                output=inspected_output,
                actual_targets=adapter_result.actual_targets,
                external_references=tuple(external_references),
                retryable=adapter_result.retryable,
                adapter_evidence=adapter_evidence,
                error_code=adapter_result.error_code,
                reason=adapter_result.reason or "validated authorized tool result",
            )
            event_type = {
                CallStatus.CANCELLED: "tool.call_cancelled",
                CallStatus.UNCERTAIN: "tool.call_uncertain",
                CallStatus.FAILED: "tool.call_failed",
            }.get(completed.status, "tool.call_completed")
            self._audit(
                context,
                call_id,
                event_type,
                completed.status.value,
                completed.reason,
                {"result_id": str(completed.id), "status": completed.status.value},
            )
            return self._complete_journaled_call(
                completed,
                request_fingerprint=request_fingerprint,
                journal_reserved=journal_reserved,
                settlement=self._effect_settlement(
                    request.effects,
                    completed,
                    dispatch_started=dispatch_started,
                ),
            )
        except CapabilityCancelled:
            terminal_result = self._terminal(
                context,
                call_id,
                started,
                CallStatus.CANCELLED,
                ErrorCode.TOOL_EFFECT,
                "tool call was cancelled",
            )
            self._audit(
                context,
                terminal_result.call_id,
                "tool.call_cancelled",
                "cancelled",
                terminal_result.reason,
                {
                    "result_id": str(terminal_result.id),
                    "status": terminal_result.status.value,
                    "error_code": terminal_result.error_code,
                },
            )
            return self._complete_journaled_call(
                terminal_result,
                request_fingerprint=request_fingerprint,
                journal_reserved=journal_reserved,
                settlement=(
                    EffectSettlement.UNCERTAIN
                    if dispatch_started and request.effects
                    else EffectSettlement.ABSENT
                ),
            )
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
                {
                    "result_id": str(terminal_result.id),
                    "status": terminal_result.status.value,
                    "error_code": terminal_result.error_code,
                },
            )
            return self._complete_journaled_call(
                terminal_result,
                request_fingerprint=request_fingerprint,
                journal_reserved=journal_reserved,
                settlement=(
                    EffectSettlement.UNCERTAIN
                    if dispatch_started and request.effects
                    else EffectSettlement.ABSENT
                ),
            )
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
            if call_id is None:
                pre_dispatch_codes.update(
                    {ErrorCode.TOOL_SCHEMA, ErrorCode.FILE, ErrorCode.SECRET_CONTENT}
                )
            status = CallStatus.REFUSED if code in pre_dispatch_codes else CallStatus.FAILED
            if dispatched_status in {CallStatus.CANCELLED, CallStatus.UNCERTAIN}:
                status = dispatched_status
            terminal_result = self._terminal(context, call_id, started, status, code, reason)
            event_type = {
                CallStatus.CANCELLED: "tool.call_cancelled",
                CallStatus.UNCERTAIN: "tool.call_uncertain",
                CallStatus.REFUSED: "tool.call_refused",
            }.get(status, "tool.call_failed")
            self._audit(
                context,
                terminal_result.call_id,
                event_type,
                status.value,
                reason,
                {
                    "result_id": str(terminal_result.id),
                    "status": terminal_result.status.value,
                    "error_code": terminal_result.error_code,
                },
            )
            return self._complete_journaled_call(
                terminal_result,
                request_fingerprint=request_fingerprint,
                journal_reserved=journal_reserved,
                settlement=(
                    EffectSettlement.UNCERTAIN
                    if dispatch_started and request.effects
                    else EffectSettlement.ABSENT
                ),
            )

    def _planned_calls_or_raise(self) -> PlannedCallJournal:
        if self._planned_calls is None:
            raise MishkanError(ErrorCode.RUN_INTERRUPTED, "planned-call journal is unavailable")
        return self._planned_calls

    def _complete_journaled_call(
        self,
        result: ToolResultEnvelope,
        *,
        request_fingerprint: str | None,
        journal_reserved: bool,
        settlement: EffectSettlement,
    ) -> ToolResultEnvelope:
        if not journal_reserved:
            return result
        assert request_fingerprint is not None
        return self._planned_calls_or_raise().complete_planned_call(
            result.call_id,
            request_fingerprint,
            result,
            settlement,
        )

    @staticmethod
    def _effect_settlement(
        declared_effects: tuple[str, ...],
        result: ToolResultEnvelope,
        *,
        dispatch_started: bool,
    ) -> EffectSettlement:
        if not declared_effects:
            return EffectSettlement.ABSENT
        if result.output is not None:
            raw = result.output.get("effect_settlement", result.output.get("settlement"))
            if isinstance(raw, str):
                try:
                    return EffectSettlement(raw)
                except ValueError:
                    pass
        if result.status is CallStatus.COMPLETED:
            return EffectSettlement.COMPLETED
        return EffectSettlement.UNCERTAIN if dispatch_started else EffectSettlement.ABSENT

    @staticmethod
    def _planned_request_fingerprint(
        context: InvocationContext,
        contract: ToolContract,
        planned_call_id: str,
        arguments: dict[str, Any],
        declared_targets: DeclaredTargets,
        authorization_fingerprint: str,
    ) -> str:
        payload = json.dumps(
            {
                "run_id": context.run_id,
                "task_attempt_id": context.task_attempt_id,
                "planned_call_id": planned_call_id,
                "tool_id": contract.tool_id,
                "tool_version": contract.version,
                "contract_fingerprint": context.binding.contract_fingerprint,
                "registry_fingerprint": context.registry.fingerprint,
                "plan_fingerprint": context.plan_fingerprint,
                "policy_fingerprint": context.policy.fingerprint,
                "authorization_fingerprint": authorization_fingerprint,
                "arguments": arguments,
                "declared_targets": declared_targets.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

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
            lexical = Path(os.path.abspath(candidate))
            if not lexical.is_relative_to(self._root):
                raise MishkanError(
                    ErrorCode.AUTHORITY_NOT_GRANTED,
                    "filesystem target is lexically outside the accepted workspace",
                    details={"path": value},
                )
            link_chain = self._link_chain(lexical)
            try:
                resolved = lexical.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise MishkanError(
                    ErrorCode.FILE,
                    "filesystem target link chain cannot be resolved",
                    details={"category": "symlink_escape", "path": value},
                ) from exc
            if not resolved.is_relative_to(self._root):
                raise MishkanError(
                    ErrorCode.AUTHORITY_NOT_GRANTED,
                    "filesystem target resolves outside the accepted workspace",
                    details={"path": value},
                )
            paths.append(
                ResolvedPath(
                    requested=value,
                    lexical_relative=lexical.relative_to(self._root).as_posix(),
                    relative=resolved.relative_to(self._root).as_posix(),
                    absolute=resolved,
                    link_chain=link_chain,
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

    def _link_chain(self, lexical: Path) -> tuple[str, ...]:
        chain: list[str] = []
        current = self._root
        for part in lexical.relative_to(self._root).parts:
            current = current / part
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    chain.append(
                        f"{current.relative_to(self._root).as_posix()}->{os.readlink(current)}"
                    )
                    self._assert_acyclic_symlink(current, lexical)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise MishkanError(
                    ErrorCode.FILE,
                    "filesystem target link chain cannot be inspected",
                    details={"category": "permission_denied", "path": lexical.as_posix()},
                ) from exc
        return tuple(chain)

    @staticmethod
    def _assert_acyclic_symlink(link: Path, requested: Path) -> None:
        seen: set[Path] = set()
        current = link
        while True:
            canonical_lexical = Path(os.path.abspath(current))
            if canonical_lexical in seen:
                raise MishkanError(
                    ErrorCode.FILE,
                    "filesystem target link chain contains a cycle",
                    details={"category": "symlink_cycle", "path": requested.as_posix()},
                )
            try:
                if not stat.S_ISLNK(canonical_lexical.lstat().st_mode):
                    return
                seen.add(canonical_lexical)
                target = Path(os.readlink(canonical_lexical))
            except FileNotFoundError:
                return
            except OSError as exc:
                raise MishkanError(
                    ErrorCode.FILE,
                    "filesystem target link chain cannot be inspected",
                    details={"category": "permission_denied", "path": requested.as_posix()},
                ) from exc
            current = target if target.is_absolute() else canonical_lexical.parent / target

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
        contract: ToolContract,
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
            scope
            for scope, values in declared.items()
            if values and scope not in contract.target_scopes
        ]
        if unsupported:
            raise MishkanError(
                ErrorCode.TOOL_SCHEMA,
                "tool invocation declared unsupported target scopes",
                details={"scopes": unsupported},
            )
        extracted_model = declared_targets_for(contract, arguments)
        extracted = {
            "path": extracted_model.paths,
            "executable": extracted_model.executables,
            "network": extracted_model.network_destinations,
            "repository": extracted_model.repositories,
            "remote": extracted_model.remotes,
            "branch": extracted_model.branches,
            "environment": extracted_model.environments,
            "external_resource": extracted_model.external_resources,
        }
        mismatched = [
            scope for scope in contract.target_scopes if declared[scope] != extracted[scope]
        ]
        if mismatched:
            raise MishkanError(
                ErrorCode.TOOL_SCHEMA,
                "declared targets do not match tool arguments",
                details={"scopes": mismatched},
            )

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

    @staticmethod
    def _validate_bound_call(allowed: tuple[str, ...], arguments: dict[str, Any]) -> None:
        if not allowed:
            return
        fingerprint = argument_fingerprint(arguments)
        if fingerprint not in allowed:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "tool arguments differ from every exact call accepted in the task plan",
                details={"argument_fingerprint": fingerprint},
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
        details: dict[str, Any] | None = None,
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
                details={
                    "registry_fingerprint": context.registry.fingerprint,
                    "plan_fingerprint": context.plan_fingerprint,
                    "policy_fingerprint": context.policy.fingerprint,
                    "binding_contract_fingerprint": context.binding.contract_fingerprint,
                    **(details or {}),
                },
            )
        )
