"""CapabilityGateway adapters for governed Browser operations."""

from __future__ import annotations

from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from mishkan.browser.models import (
    BrowserActionKind,
    BrowserActionRequest,
    BrowserDiagnosticRequest,
    BrowserObservationRequest,
    BrowserSessionRequest,
)
from mishkan.browser.service import BrowserSupervisor
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.tools.adapters import AdapterCall, CapabilityAdapter
from mishkan.tools.gateway_models import AdapterResult

RequestModel = TypeVar("RequestModel", bound=BaseModel)


class _BrowserToolAdapter:
    adapter_id: str

    def __init__(self, supervisor: BrowserSupervisor) -> None:
        self._supervisor = supervisor

    @staticmethod
    def _request(call: AdapterCall, model: type[RequestModel]) -> RequestModel:
        try:
            return model.model_validate(call.arguments["request"])
        except (KeyError, ValidationError) as exc:
            raise MishkanError(
                ErrorCode.TOOL_SCHEMA,
                "Browser tool request does not match its public schema",
            ) from exc

    @staticmethod
    def _string_list(call: AdapterCall, name: str) -> tuple[str, ...]:
        value = call.arguments.get(name)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise MishkanError(ErrorCode.TOOL_SCHEMA, f"Browser {name} must be a string list")
        return tuple(value)

    @staticmethod
    def _verify_identity(call: AdapterCall, request: BrowserSessionRequest) -> None:
        if (
            request.owner_identity != call.acting_identity
            or request.run_id != call.run_id
            or request.task_attempt_id != call.task_attempt_id
        ):
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "Browser session identity differs from its authorized invocation",
            )

    @staticmethod
    def _verify_targets(
        call: AdapterCall,
        *,
        paths: tuple[str, ...] = (),
        network: tuple[str, ...] = (),
        external: tuple[str, ...] = (),
    ) -> None:
        actual_paths = tuple(item.requested for item in call.targets.paths)
        if actual_paths != paths:
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "Browser path targets differ")
        if call.targets.network_destinations != network:
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "Browser network targets differ")
        if call.targets.external_resources != external:
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "Browser external targets differ")

    @staticmethod
    def _verify_effects(call: AdapterCall, expected: tuple[str, ...]) -> None:
        if _BrowserToolAdapter._string_list(call, "declared_effects") != expected:
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "Browser declared effects differ")

    @staticmethod
    def _result(
        call: AdapterCall,
        value: BaseModel,
        references: tuple[str, ...] = (),
    ) -> AdapterResult:
        return AdapterResult(
            output=value.model_dump(mode="json"),
            actual_targets=call.targets,
            external_references=tuple(dict.fromkeys(references)),
            evidence={"adapter": "mishkan.browser", "operation": call.capability},
        )

    @staticmethod
    def _session_resource(session_id: object) -> str:
        return f"browser:{session_id}"

    @staticmethod
    def _origin(raw_url: str) -> str:
        url = httpx.URL(raw_url)
        if url.scheme not in {"http", "https"} or url.host is None or url.userinfo:
            raise MishkanError(ErrorCode.BROWSER, "Browser URL has no valid HTTP origin")
        port = url.port or (443 if url.scheme == "https" else 80)
        default = (url.scheme == "https" and port == 443) or (url.scheme == "http" and port == 80)
        return f"{url.scheme}://{url.host}" if default else f"{url.scheme}://{url.host}:{port}"


class BrowserOpenToolAdapter(_BrowserToolAdapter):
    adapter_id = "native.browser.open"

    def invoke(self, call: AdapterCall) -> AdapterResult:
        request = self._request(call, BrowserSessionRequest)
        self._verify_identity(call, request)
        network = (self._origin(str(request.initial_url)),) if request.initial_url else ()
        self._verify_targets(call, paths=(request.workspace,), network=network)
        self._verify_effects(call, ("browser.session.open",))
        if call.credentials:
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "Browser open has undeclared credentials")
        session = self._supervisor.open(request)
        resource = self._session_resource(session.id)
        return self._result(call, session, (resource,))


class BrowserObserveToolAdapter(_BrowserToolAdapter):
    adapter_id = "native.browser.observe"

    def invoke(self, call: AdapterCall) -> AdapterResult:
        request = self._request(call, BrowserObservationRequest)
        resource = self._session_resource(request.session_id)
        self._verify_targets(call, external=(resource,))
        self._verify_effects(call, ("browser.observe",))
        if call.credentials:
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "Browser observe has undeclared credentials")
        result = self._supervisor.observe(request, owner_identity=call.acting_identity)
        references = tuple(
            item
            for item in (
                resource,
                result.tree_artifact_reference,
                result.screenshot_artifact_reference,
            )
            if item is not None
        )
        return self._result(call, result, references)


class BrowserActToolAdapter(_BrowserToolAdapter):
    adapter_id = "native.browser.act"

    def invoke(self, call: AdapterCall) -> AdapterResult:
        request = self._request(call, BrowserActionRequest)
        resource = self._session_resource(request.session_id)
        network = (
            (self._origin(request.value),)
            if request.kind is BrowserActionKind.NAVIGATE and isinstance(request.value, str)
            else ()
        )
        paths = self._string_list(call, "paths") if request.kind is BrowserActionKind.UPLOAD else ()
        expected_paths = (
            self._upload_values(request) if request.kind is BrowserActionKind.UPLOAD else ()
        )
        if paths != expected_paths:
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "Browser upload paths differ")
        external = tuple(
            item
            for item in (resource, request.visual_evidence_artifact_reference)
            if item is not None
        )
        self._verify_targets(call, paths=paths, network=network, external=external)
        self._verify_effects(call, (request.resolved_effect,))
        credential_refs = self._string_list(call, "credential_refs")
        expected_refs = (
            (request.credential_reference,) if request.credential_reference is not None else ()
        )
        if credential_refs != expected_refs or tuple(call.credentials) != expected_refs:
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "Browser credential declarations differ")
        result = self._supervisor.act(
            request,
            owner_identity=call.acting_identity,
            credential_values=call.credentials,
        )
        return self._result(call, result, (resource, *result.artifact_references))

    @staticmethod
    def _upload_values(request: BrowserActionRequest) -> tuple[str, ...]:
        value = request.value
        if isinstance(value, str):
            return (value,)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return tuple(value)
        raise MishkanError(ErrorCode.TOOL_SCHEMA, "Browser upload request paths are invalid")


class BrowserDiagnosticsToolAdapter(_BrowserToolAdapter):
    adapter_id = "native.browser.diagnostics"

    def invoke(self, call: AdapterCall) -> AdapterResult:
        request = self._request(call, BrowserDiagnosticRequest)
        resource = self._session_resource(request.session_id)
        self._verify_targets(call, external=(resource,))
        self._verify_effects(call, ("browser.diagnostics",))
        if call.credentials:
            raise MishkanError(
                ErrorCode.TOOL_SCHEMA,
                "Browser diagnostics has undeclared credentials",
            )
        result = self._supervisor.diagnostics(request, owner_identity=call.acting_identity)
        return self._result(call, result, (resource, result.artifact_reference))


class BrowserCloseToolAdapter(_BrowserToolAdapter):
    adapter_id = "native.browser.close"

    def invoke(self, call: AdapterCall) -> AdapterResult:
        raw = call.arguments.get("session_id")
        if not isinstance(raw, str):
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "Browser close requires a session UUID")
        resource = self._session_resource(raw)
        self._verify_targets(call, external=(resource,))
        self._verify_effects(call, ("browser.session.close",))
        if call.credentials:
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "Browser close has undeclared credentials")
        try:
            from uuid import UUID

            session_id = UUID(raw)
        except ValueError as exc:
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "Browser session UUID is invalid") from exc
        result = self._supervisor.close(session_id, owner_identity=call.acting_identity)
        return self._result(call, result, (resource,))


def build_browser_tool_adapters(
    supervisor: BrowserSupervisor,
) -> dict[str, CapabilityAdapter]:
    adapters = (
        BrowserOpenToolAdapter(supervisor),
        BrowserObserveToolAdapter(supervisor),
        BrowserActToolAdapter(supervisor),
        BrowserDiagnosticsToolAdapter(supervisor),
        BrowserCloseToolAdapter(supervisor),
    )
    return {adapter.adapter_id: adapter for adapter in adapters}
