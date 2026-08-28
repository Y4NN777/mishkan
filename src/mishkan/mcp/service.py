"""Governed MCP connection and invocation orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID

import httpx
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]
from mcp.shared.session import ProgressFnT

from mishkan.config.models import McpConfig, McpConnectionConfig
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.mcp.models import (
    McpCallRequest,
    McpCallResult,
    McpCallState,
    McpClientCallOutcome,
    McpConnectionRecord,
    McpDirection,
    McpDiscoverySnapshot,
    McpEffectDisposition,
    McpPrimitiveDescriptor,
    McpPrimitiveKind,
    McpProgressEvent,
    McpRemoteTaskTerminal,
    McpSessionState,
)
from mishkan.mcp.repository import McpRepository
from mishkan.tools.inspection import ContentInspector


class McpClientPort(Protocol):
    async def discover(
        self,
        connection_id: str,
        configured: McpConnectionConfig,
        *,
        credentials: Mapping[str, str],
        workspace: Path,
    ) -> McpDiscoverySnapshot: ...

    async def call_tool(
        self,
        configured: McpConnectionConfig,
        *,
        name: str,
        arguments: dict[str, Any],
        caller_identity: str,
        run_id: str,
        task_attempt_id: str,
        timeout_seconds: float,
        credentials: Mapping[str, str],
        workspace: Path,
        progress: ProgressFnT,
        remote_task_allowed: bool,
        remote_task_id: str | None,
        remote_task_started: Callable[[str], None],
        task_poll_min_seconds: float,
        task_poll_max_seconds: float,
    ) -> McpClientCallOutcome: ...

    async def cancel_remote_task(
        self,
        configured: McpConnectionConfig,
        *,
        remote_task_id: str,
        timeout_seconds: float,
        credentials: Mapping[str, str],
        workspace: Path,
    ) -> McpClientCallOutcome: ...


class McpService:
    """Keep transport work outside transactions while journaling every boundary."""

    def __init__(
        self,
        workspace: Path,
        config: McpConfig,
        repository: McpRepository,
        client: McpClientPort,
        inspector: ContentInspector,
    ) -> None:
        self._workspace = workspace.resolve()
        self._config = config
        self._repository = repository
        self._client = client
        self._inspector = inspector
        self._active: dict[UUID, asyncio.Task[Any]] = {}

    async def connect(
        self,
        connection_id: str,
        *,
        principal: str,
        policy_fingerprint: str,
        credentials: Mapping[str, str],
    ) -> McpConnectionRecord:
        configured = self._configured(connection_id)
        current = self._repository.find_connection(connection_id)
        if current is None:
            now = utc_now()
            working = McpConnectionRecord(
                connection_id=connection_id,
                direction=McpDirection.OUTBOUND,
                transport=configured.transport,
                protocol_strategy=configured.protocol_strategy,
                configured_protocol_versions=configured.protocol_versions,
                trust=configured.trust,
                exposure_profile=configured.exposure_profile,
                remote_tasks_enabled=configured.remote_tasks_enabled,
                server_identity=self._server_identity(configured),
                credential_references=tuple(
                    reference.locator for reference in configured.credential_refs
                ),
                credential_principal=principal,
                policy_fingerprint=policy_fingerprint,
                state=McpSessionState.STARTING,
                revision=0,
                health="starting",
                created_at=now,
                updated_at=now,
            )
            self._repository.create_connection(working)
            bound_fingerprint = None
        else:
            self._require_compatible_configuration(current, configured)
            working = current.model_copy(
                update={
                    "state": McpSessionState.RECONNECTING,
                    "revision": current.revision + 1,
                    "credential_principal": principal,
                    "policy_fingerprint": policy_fingerprint,
                    "health": "connecting",
                    "last_error": None,
                    "updated_at": utc_now(),
                }
            )
            self._repository.update_connection(working, expected_revision=current.revision)
            bound_fingerprint = current.schema_fingerprint
        try:
            snapshot = await self._client.discover(
                connection_id,
                configured,
                credentials=credentials,
                workspace=self._workspace,
            )
            self._repository.replace_discovery(
                snapshot,
                expected_connection_revision=working.revision,
                expected_schema_fingerprint=bound_fingerprint,
            )
        except Exception as exc:
            self._mark_connection_failure(connection_id, exc)
            if isinstance(exc, MishkanError):
                raise
            raise MishkanError(
                ErrorCode.MCP,
                "MCP connection discovery failed",
                details={"connection_id": connection_id, "reason": type(exc).__name__},
                retryable=True,
            ) from exc
        discovered = self._repository.get_connection(connection_id)
        ready = discovered.model_copy(
            update={
                "state": McpSessionState.READY,
                "revision": discovered.revision + 1,
                "health": "healthy",
                "last_error": None,
                "updated_at": utc_now(),
            }
        )
        return self._repository.update_connection(ready, expected_revision=discovered.revision)

    async def invoke(
        self,
        request: McpCallRequest,
        *,
        credentials: Mapping[str, str],
    ) -> McpCallResult:
        configured, primitive = self._validate_call(request)
        reservation = self._repository.reserve_call(request)
        if not reservation.created:
            if reservation.existing_result is not None:
                return reservation.existing_result
            raise MishkanError(
                ErrorCode.DUPLICATE_RESULT,
                "MCP call with this idempotency key is still in progress",
                retryable=True,
            )
        return await self._execute(
            request,
            configured,
            primitive,
            credentials=credentials,
            remote_task_id=None,
            reconciling=False,
        )

    async def resume_remote_task(
        self,
        request_id: UUID,
        *,
        credentials: Mapping[str, str],
    ) -> McpCallResult:
        binding = self._repository.get_remote_task(request_id)
        configured, primitive = self._validate_call(binding.request)
        return await self._execute(
            binding.request,
            configured,
            primitive,
            credentials=credentials,
            remote_task_id=binding.remote_task_id,
            reconciling=True,
        )

    async def _execute(
        self,
        request: McpCallRequest,
        configured: McpConnectionConfig,
        primitive: McpPrimitiveDescriptor,
        *,
        credentials: Mapping[str, str],
        remote_task_id: str | None,
        reconciling: bool,
    ) -> McpCallResult:
        remaining = (request.deadline - utc_now()).total_seconds()
        if remaining <= 0 and not reconciling:
            return self._complete_failure(request, McpCallState.FAILED, "MCP deadline expired")
        timeout_seconds = (
            configured.call_timeout_seconds
            if reconciling
            else min(remaining, configured.call_timeout_seconds)
        )
        task = asyncio.current_task()
        if task is not None:
            self._active[request.id] = task
        existing_progress = self._repository.progress_after(request.id, 0)
        cursor = existing_progress[-1].cursor + 1 if existing_progress else 0

        async def progress(value: float, total: float | None, message: str | None) -> None:
            nonlocal cursor
            self._repository.append_progress(
                McpProgressEvent(
                    request_id=request.id,
                    cursor=cursor,
                    progress=value,
                    total=total,
                    message=self._inspector.inspect(message) if message is not None else None,
                )
            )
            cursor += 1

        try:
            if not reconciling:
                self._repository.set_call_state(request.id, McpCallState.DISPATCHING)
                self._repository.set_call_state(request.id, McpCallState.RUNNING)
            outcome = await self._client.call_tool(
                configured,
                name=request.primitive_name,
                arguments=request.arguments,
                caller_identity=request.caller_identity,
                run_id=request.run_id,
                task_attempt_id=request.task_attempt_id,
                timeout_seconds=timeout_seconds,
                credentials=credentials,
                workspace=self._workspace,
                progress=cast(ProgressFnT, progress),
                remote_task_allowed=request.remote_task_allowed,
                remote_task_id=remote_task_id,
                remote_task_started=lambda value: self._repository.attach_remote_task(
                    request.id, value
                ),
                task_poll_min_seconds=self._config.task_poll_min_seconds,
                task_poll_max_seconds=self._config.task_poll_max_seconds,
            )
            normalized = self._clean_output(outcome, credentials)
            self._validate_output(primitive, normalized)
            state = {
                McpRemoteTaskTerminal.IMMEDIATE: McpCallState.COMPLETED,
                McpRemoteTaskTerminal.COMPLETED: McpCallState.COMPLETED,
                McpRemoteTaskTerminal.FAILED: McpCallState.FAILED,
                McpRemoteTaskTerminal.CANCELLED: McpCallState.CANCELLED,
            }[outcome.terminal]
            return self._repository.complete_call(
                McpCallResult(
                    request_id=request.id,
                    connection_id=request.connection_id,
                    primitive_name=request.primitive_name,
                    state=state,
                    output=normalized,
                    remote_task_id=outcome.remote_task_id,
                    schema_hash=request.expected_schema_hash,
                    reason=self._inspector.inspect(
                        outcome.reason,
                        tuple(credentials.values()),
                    ),
                )
            )
        except asyncio.CancelledError:
            if self._remote_task_id(request.id) is not None:
                raise MishkanError(
                    ErrorCode.MCP,
                    "remote MCP task remains pending after local cancellation",
                    retryable=True,
                ) from None
            return self._complete_interrupted(request, cancelled=True)
        except Exception as exc:
            remote_identity = self._remote_task_id(request.id)
            if isinstance(exc, MishkanError) and exc.envelope.code in {
                ErrorCode.SECRET_CONTENT,
                ErrorCode.TOOL_SCHEMA,
            }:
                return self._complete_failure(
                    request,
                    McpCallState.FAILED,
                    "MCP result failed configured validation or content inspection",
                    remote_task_id=remote_identity,
                )
            if remote_identity is not None:
                if isinstance(exc, MishkanError):
                    raise
                raise MishkanError(
                    ErrorCode.MCP,
                    "remote MCP task remains recoverable after transport interruption",
                    details={"reason": type(exc).__name__},
                    retryable=True,
                ) from exc
            return self._complete_interrupted(request, cancelled=False)
        finally:
            self._active.pop(request.id, None)

    def request_cancellation(self, request_id: UUID) -> None:
        self._repository.set_call_state(request_id, McpCallState.CANCEL_REQUESTED)
        task = self._active.get(request_id)
        if task is not None:
            task.cancel()

    async def cancel_remote_task(
        self,
        request_id: UUID,
        *,
        credentials: Mapping[str, str],
    ) -> McpCallResult:
        binding = self._repository.get_remote_task(request_id)
        configured, _primitive = self._validate_call(binding.request)
        connection = self._repository.get_connection(binding.request.connection_id)
        if not connection.task_cancellation_supported:
            raise MishkanError(
                ErrorCode.MCP,
                "remote MCP task cancellation was not negotiated",
            )
        self._repository.set_call_state(request_id, McpCallState.CANCEL_REQUESTED)
        outcome = await self._client.cancel_remote_task(
            configured,
            remote_task_id=binding.remote_task_id,
            timeout_seconds=configured.call_timeout_seconds,
            credentials=credentials,
            workspace=self._workspace,
        )
        if outcome.terminal is not McpRemoteTaskTerminal.CANCELLED:
            raise MishkanError(
                ErrorCode.MCP,
                "remote MCP task cancellation was not confirmed",
                retryable=True,
            )
        return self._repository.complete_call(
            McpCallResult(
                request_id=binding.request.id,
                connection_id=binding.request.connection_id,
                primitive_name=binding.request.primitive_name,
                state=McpCallState.CANCELLED,
                remote_task_id=binding.remote_task_id,
                schema_hash=binding.request.expected_schema_hash,
                reason=self._inspector.inspect(
                    outcome.reason,
                    tuple(credentials.values()),
                ),
            )
        )

    def reconcile_after_restart(self) -> tuple[McpCallResult, ...]:
        return self._repository.reconcile_incomplete()

    def remote_task_connection_id(self, request_id: UUID) -> str:
        return self._repository.get_remote_task(request_id).request.connection_id

    def call_connection_id(self, request_id: UUID) -> str:
        return self._repository.call_connection_id(request_id)

    def _validate_call(
        self,
        request: McpCallRequest,
    ) -> tuple[McpConnectionConfig, McpPrimitiveDescriptor]:
        configured = self._configured(request.connection_id)
        connection = self._repository.get_connection(request.connection_id)
        if connection.state is not McpSessionState.READY:
            raise MishkanError(ErrorCode.MCP, "MCP connection is not ready", retryable=True)
        if request.remote_task_allowed and (
            not configured.remote_tasks_enabled or not connection.task_tool_calls_supported
        ):
            raise MishkanError(
                ErrorCode.MCP,
                "MCP remote task execution is not configured and negotiated",
            )
        primitive = self._repository.require_primitive(
            request.connection_id,
            request.primitive_name,
            request.expected_schema_hash,
            kind=McpPrimitiveKind.TOOL,
        )
        if request.effect_disposition is not primitive.effect_disposition:
            raise MishkanError(
                ErrorCode.TOOL_DRIFT,
                "MCP call effect disposition differs from discovery evidence",
            )
        if primitive.input_schema is not None:
            try:
                Draft202012Validator.check_schema(primitive.input_schema)
                Draft202012Validator(primitive.input_schema).validate(request.arguments)
            except (SchemaError, ValidationError) as exc:
                raise MishkanError(
                    ErrorCode.TOOL_SCHEMA,
                    "MCP call arguments differ from the discovered input schema",
                ) from exc
        return configured, primitive

    def _clean_output(
        self,
        outcome: McpClientCallOutcome,
        credentials: Mapping[str, str],
    ) -> dict[str, Any] | None:
        if outcome.output is None:
            return None
        serialized = json.dumps(outcome.output, sort_keys=True, separators=(",", ":"))
        cleaned = self._inspector.inspect(serialized, tuple(credentials.values()))
        normalized = json.loads(cleaned)
        if not isinstance(normalized, dict):
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "MCP result envelope is not an object")
        return normalized

    @staticmethod
    def _validate_output(
        primitive: McpPrimitiveDescriptor,
        output: dict[str, Any] | None,
    ) -> None:
        if primitive.output_schema is None or output is None:
            return
        structured = output.get("structuredContent")
        try:
            Draft202012Validator.check_schema(primitive.output_schema)
            Draft202012Validator(primitive.output_schema).validate(structured)
        except (SchemaError, ValidationError) as exc:
            raise MishkanError(
                ErrorCode.TOOL_SCHEMA,
                "MCP structured result differs from the discovered output schema",
            ) from exc

    def _remote_task_id(self, request_id: UUID) -> str | None:
        try:
            return self._repository.get_remote_task(request_id).remote_task_id
        except MishkanError:
            return None

    def _configured(self, connection_id: str) -> McpConnectionConfig:
        configured = self._config.connections.get(connection_id)
        if configured is None or not configured.enabled:
            raise MishkanError(ErrorCode.MCP, "MCP connection is not enabled")
        return configured

    @staticmethod
    def _require_compatible_configuration(
        record: McpConnectionRecord,
        configured: McpConnectionConfig,
    ) -> None:
        if (
            record.transport is not configured.transport
            or record.protocol_strategy is not configured.protocol_strategy
            or record.configured_protocol_versions != configured.protocol_versions
            or record.trust != configured.trust
            or record.exposure_profile != configured.exposure_profile
            or record.remote_tasks_enabled != configured.remote_tasks_enabled
            or record.server_identity != McpService._server_identity(configured)
            or record.credential_references
            != tuple(reference.locator for reference in configured.credential_refs)
        ):
            raise MishkanError(
                ErrorCode.TOOL_DRIFT,
                "MCP connection configuration changed after binding",
            )

    @staticmethod
    def _server_identity(configured: McpConnectionConfig) -> str:
        if configured.command is not None:
            arguments = json.dumps(configured.arguments, separators=(",", ":")).encode()
            return f"stdio:{configured.command}#sha256:{hashlib.sha256(arguments).hexdigest()}"
        assert configured.endpoint is not None
        endpoint = httpx.URL(str(configured.endpoint))
        port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
        default = (endpoint.scheme == "https" and port == 443) or (
            endpoint.scheme == "http" and port == 80
        )
        authority = endpoint.host if default else f"{endpoint.host}:{port}"
        return f"http:{endpoint.scheme}://{authority}{endpoint.path}"

    def _mark_connection_failure(self, connection_id: str, error: Exception) -> None:
        current = self._repository.get_connection(connection_id)
        degraded = current.schema_fingerprint is not None
        failed = current.model_copy(
            update={
                "state": McpSessionState.DEGRADED if degraded else McpSessionState.FAILED,
                "revision": current.revision + 1,
                "health": "degraded" if degraded else "failed",
                "last_error": type(error).__name__,
                "updated_at": utc_now(),
            }
        )
        self._repository.update_connection(failed, expected_revision=current.revision)

    def _complete_interrupted(
        self,
        request: McpCallRequest,
        *,
        cancelled: bool,
    ) -> McpCallResult:
        uncertain = request.effect_disposition in {
            McpEffectDisposition.NON_IDEMPOTENT,
            McpEffectDisposition.UNKNOWN,
        }
        state = (
            McpCallState.UNCERTAIN
            if uncertain
            else McpCallState.CANCELLED
            if cancelled
            else McpCallState.LOST
        )
        return self._complete_failure(
            request,
            state,
            (
                "MCP call ended after an indeterminate external effect"
                if uncertain
                else "MCP call was cancelled before an accepted result"
                if cancelled
                else "MCP transport ended before an accepted result"
            ),
        )

    def _complete_failure(
        self,
        request: McpCallRequest,
        state: McpCallState,
        reason: str,
        *,
        remote_task_id: str | None = None,
    ) -> McpCallResult:
        return self._repository.complete_call(
            McpCallResult(
                request_id=request.id,
                connection_id=request.connection_id,
                primitive_name=request.primitive_name,
                state=state,
                remote_task_id=remote_task_id,
                schema_hash=request.expected_schema_hash,
                error_code=ErrorCode.MCP,
                reason=reason,
            )
        )
