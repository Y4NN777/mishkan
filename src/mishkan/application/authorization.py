"""Server-derived authorization scopes for authoritative application commands.

Command semantics live here as public, versioned contracts. Operational grants do
not: they remain in the configured public policy documents.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import ValidationError

from mishkan.application.contracts import ApplicationCommand, RunInitializationRequest
from mishkan.config.models import (
    McpConfig,
    McpConnectionConfig,
    McpTransport,
    MishkanConfig,
)
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.edits import ChangeSet
from mishkan.execution import SessionRecord, SessionRequest
from mishkan.policy import (
    AuthorizationDecision,
    AuthorizationRequest,
    Decision,
    EffectivePolicy,
    PolicyAuthority,
    PolicyLoader,
    ResourceRequest,
)


class ChangeSetLookup(Protocol):
    def definition(self, change_set_id: UUID) -> ChangeSet: ...


class SessionLookup(Protocol):
    def status(self, session_id: UUID) -> SessionRecord: ...


class McpCallLookup(Protocol):
    def call_connection_id(self, request_id: UUID) -> str: ...


@dataclass(frozen=True, slots=True)
class CommandSemantics:
    capability: str
    effect_class: str
    effects: tuple[str, ...]
    network: bool = False


# This registry describes immutable command meaning. It is deliberately not an
# operational allow-list; PolicyAuthority remains closed-world and decides grants.
COMMAND_SEMANTICS = MappingProxyType(
    {
        "system.checkpoint": CommandSemantics(
            "application.system.checkpoint", "control", ("state.checkpoint",)
        ),
        "run.initialize": CommandSemantics(
            "application.run.initialize", "coordination", ("run.initialize",), True
        ),
        "run.cancel": CommandSemantics("application.run.control", "coordination", ("run.cancel",)),
        "run.recover": CommandSemantics(
            "application.run.control", "coordination", ("run.recover",)
        ),
        "artifact.upload.open": CommandSemantics(
            "application.artifact.upload", "artifact", ("artifact.stage",)
        ),
        "artifact.upload.chunk": CommandSemantics(
            "application.artifact.upload", "artifact", ("artifact.append",)
        ),
        "artifact.upload.commit": CommandSemantics(
            "application.artifact.upload", "artifact", ("artifact.publish",)
        ),
        "artifact.reference.update": CommandSemantics(
            "application.artifact.reference", "artifact", ("artifact.reference.update",)
        ),
        "artifact.gc.plan": CommandSemantics(
            "application.artifact.gc", "artifact", ("artifact.gc.plan",)
        ),
        "artifact.gc.apply": CommandSemantics(
            "application.artifact.gc", "artifact", ("artifact.gc.apply",)
        ),
        "change.plan": CommandSemantics("application.change.plan", "filesystem", ("change.plan",)),
        "change.apply": CommandSemantics(
            "application.change.apply", "filesystem", ("change.apply",)
        ),
        "change.reconcile": CommandSemantics(
            "application.change.reconcile", "filesystem", ("change.reconcile",)
        ),
        "session.start": CommandSemantics(
            "application.session.start", "process", ("process.start",)
        ),
        "session.write": CommandSemantics("application.session.io", "process", ("process.input",)),
        "session.resize": CommandSemantics(
            "application.session.io", "process", ("process.resize",)
        ),
        "session.signal": CommandSemantics(
            "application.session.control", "process", ("process.signal",)
        ),
        "session.cancel": CommandSemantics(
            "application.session.control", "process", ("process.cancel",)
        ),
        "session.settle": CommandSemantics(
            "application.session.settle", "process", ("process.settle",)
        ),
        "mcp.connection.connect": CommandSemantics(
            "application.mcp.connect", "external", ("mcp.connect",), True
        ),
        "mcp.call.cancel": CommandSemantics(
            "application.mcp.control", "external", ("mcp.call.cancel",), True
        ),
        "mcp.call.reconcile": CommandSemantics(
            "application.mcp.control", "external", ("mcp.call.reconcile",), True
        ),
    }
)

_COMMAND_TARGETS = MappingProxyType(
    {
        "system.checkpoint": ("system", "optional"),
        "run.initialize": ("run", "absent"),
        "run.cancel": ("run", "required"),
        "run.recover": ("run", "required"),
        "artifact.upload.open": ("artifact_service", "absent"),
        "artifact.upload.chunk": ("artifact_upload", "uuid"),
        "artifact.upload.commit": ("artifact_upload", "uuid"),
        "artifact.reference.update": ("artifact_reference", "absent"),
        "artifact.gc.plan": ("artifact_service", "absent"),
        "artifact.gc.apply": ("artifact_gc_plan", "uuid"),
        "change.plan": ("change_set", "uuid"),
        "change.apply": ("change_set", "uuid"),
        "change.reconcile": ("change_set", "uuid"),
        "session.start": ("session_service", "absent"),
        "session.write": ("session", "uuid"),
        "session.resize": ("session", "uuid"),
        "session.signal": ("session", "uuid"),
        "session.cancel": ("session", "uuid"),
        "session.settle": ("session", "uuid"),
        "mcp.connection.connect": ("mcp_connection", "required"),
        "mcp.call.cancel": ("mcp_call", "uuid"),
        "mcp.call.reconcile": ("mcp_call", "uuid"),
    }
)

_COMMAND_PAYLOAD_FIELDS = MappingProxyType(
    {
        "system.checkpoint": (frozenset(), frozenset({"checkpoint", "index"})),
        "run.initialize": (frozenset({"objective"}), frozenset({"schema_version"})),
        "run.cancel": (frozenset(), frozenset()),
        "run.recover": (frozenset(), frozenset({"uncertain_effects"})),
        "artifact.upload.open": (
            frozenset({"expected_size", "expected_digest", "media_type", "provenance"}),
            frozenset({"sensitivity", "retention"}),
        ),
        "artifact.upload.chunk": (
            frozenset({"offset", "content_base64"}),
            frozenset(),
        ),
        "artifact.upload.commit": (frozenset(), frozenset()),
        "artifact.reference.update": (
            frozenset({"scope", "name", "artifact_reference", "expected_reference_revision"}),
            frozenset(),
        ),
        "artifact.gc.plan": (frozenset({"watermark"}), frozenset()),
        "artifact.gc.apply": (frozenset(), frozenset()),
        "change.plan": (frozenset({"change_set"}), frozenset()),
        "change.apply": (frozenset(), frozenset()),
        "change.reconcile": (frozenset(), frozenset()),
        "session.start": (frozenset({"request"}), frozenset()),
        "session.write": (frozenset({"content_base64"}), frozenset()),
        "session.resize": (frozenset({"rows", "columns"}), frozenset()),
        "session.signal": (frozenset({"signal"}), frozenset()),
        "session.cancel": (frozenset(), frozenset()),
        "session.settle": (frozenset(), frozenset()),
        "mcp.connection.connect": (frozenset(), frozenset()),
        "mcp.call.cancel": (frozenset(), frozenset()),
        "mcp.call.reconcile": (frozenset(), frozenset()),
    }
)


@dataclass(frozen=True, slots=True)
class AuthorizedApplicationCommand:
    command: ApplicationCommand
    request: AuthorizationRequest
    decision: AuthorizationDecision
    session_request: SessionRequest | None = None


class ApplicationCommandAuthority:
    """Validate command meaning and evaluate its server-derived exact scope."""

    def __init__(
        self,
        config: MishkanConfig,
        workspace: Path,
        changes: ChangeSetLookup,
        sessions: SessionLookup,
        mcp_calls: McpCallLookup | None = None,
    ) -> None:
        self._config = config
        self._workspace = workspace.resolve(strict=True)
        self._changes = changes
        self._sessions = sessions
        self._mcp_calls = mcp_calls
        self._policy: EffectivePolicy = PolicyLoader().load(config.policy_sources, self._workspace)

    @property
    def policy(self) -> EffectivePolicy:
        return self._policy

    def authorize(self, command: ApplicationCommand) -> AuthorizedApplicationCommand:
        # Deep-normalize the mutable payload before deriving authority and dispatching.
        normalized = ApplicationCommand.model_validate_json(command.model_dump_json())
        semantics = COMMAND_SEMANTICS.get(normalized.command_type)
        if semantics is None:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "application command type has no public authorization contract",
                details={"command_type": normalized.command_type},
            )
        self._validate_envelope(normalized)

        paths: tuple[str, ...] = ()
        executables: tuple[str, ...] = ()
        arguments: tuple[str, ...] = (normalized.command_type,)
        environments: tuple[str, ...] = ()
        credentials: tuple[str, ...] = ()
        external_resources: tuple[str, ...] = ()
        network_destinations: tuple[str, ...] = ()
        effects = semantics.effects
        timeout = 120
        session_request: SessionRequest | None = None

        try:
            if normalized.command_type == "run.initialize":
                RunInitializationRequest.model_validate(normalized.payload)
                timeout = self._config.crewai.model_timeout_seconds
            elif normalized.command_type == "change.plan":
                change_set = ChangeSet.model_validate(normalized.payload["change_set"])
                if normalized.target_id != str(change_set.id):
                    raise MishkanError(
                        ErrorCode.OUTPUT_CONTRACT,
                        "change plan target differs from its immutable change-set identity",
                    )
                paths, effects = self._change_scope(change_set, semantics.effects)
            elif normalized.command_type in {"change.apply", "change.reconcile"}:
                change_set = self._changes.definition(self._target_uuid(normalized))
                paths, effects = self._change_scope(change_set, semantics.effects)
            elif normalized.command_type == "session.start":
                session_request = SessionRequest.model_validate(normalized.payload["request"])
                if session_request.owner != normalized.actor_id:
                    raise MishkanError(
                        ErrorCode.AUTHORITY_NOT_GRANTED,
                        "session owner must match the authenticated command actor",
                    )
                paths = (session_request.workspace,)
                executables = (session_request.executable,)
                arguments = session_request.arguments
                environments = tuple(sorted(session_request.environment))
                credentials = tuple(
                    sorted(
                        {
                            *(
                                item.locator
                                for item in session_request.credential_environment.values()
                            ),
                            *(item.locator for item in session_request.credential_references),
                        }
                    )
                )
                timeout = self._remaining_seconds(session_request.deadline)
            elif normalized.command_type.startswith("session."):
                record = self._sessions.status(self._target_uuid(normalized))
                if record.owner != normalized.actor_id:
                    raise MishkanError(
                        ErrorCode.AUTHORITY_NOT_GRANTED,
                        "session command actor does not own the target session",
                    )
                external_resources = (f"session:{record.session_id}",)
            elif normalized.command_type == "mcp.connection.connect":
                connection = self._mcp_connection(normalized)
                credentials = tuple(sorted(item.locator for item in connection.credential_refs))
                timeout = math.ceil(connection.connect_timeout_seconds)
                external_resources = (f"mcp-connection:{normalized.target_id}",)
                if connection.transport is McpTransport.STDIO:
                    assert connection.command is not None
                    executables = (connection.command,)
                    arguments = connection.arguments
                    environments = tuple(sorted(connection.inherit_environment))
                else:
                    assert connection.endpoint is not None
                    network_destinations = (self._network_destination(str(connection.endpoint)),)
            elif normalized.command_type.startswith("mcp.call."):
                request_id = self._target_uuid(normalized)
                if self._mcp_calls is None:
                    raise MishkanError(ErrorCode.MCP, "MCP mediation is not configured")
                connection_id = self._mcp_calls.call_connection_id(request_id)
                connection = self._configured_mcp_connection(connection_id)
                credentials = tuple(sorted(item.locator for item in connection.credential_refs))
                timeout = math.ceil(connection.call_timeout_seconds)
                external_resources = (
                    f"mcp-call:{request_id}",
                    f"mcp-connection:{connection_id}",
                )
                if connection.transport is McpTransport.STDIO:
                    assert connection.command is not None
                    executables = (connection.command,)
                else:
                    assert connection.endpoint is not None
                    network_destinations = (self._network_destination(str(connection.endpoint)),)
            elif normalized.target_id is not None:
                external_resources = (f"{normalized.target_type}:{normalized.target_id}",)
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "application command payload does not match its authorization contract",
                details={"command_type": normalized.command_type},
            ) from exc

        request = AuthorizationRequest(
            plan_fingerprint=normalized.fingerprint,
            identity=normalized.actor_id,
            objective_class="application-command",
            repository=str(self._workspace),
            outcome=normalized.command_type,
            role="application-client",
            capability=semantics.capability,
            effect_class=semantics.effect_class,
            effects=effects,
            paths=paths,
            executables=executables,
            arguments=arguments,
            network_destinations=network_destinations,
            environments=environments,
            credentials=credentials,
            external_resources=external_resources,
            resources=ResourceRequest(
                timeout_seconds=max(1, min(timeout, 86_400)),
                network=semantics.network,
            ),
        )
        decision = PolicyAuthority().evaluate(request, self._policy)
        return AuthorizedApplicationCommand(normalized, request, decision, session_request)

    @staticmethod
    def is_allowed(authorized: AuthorizedApplicationCommand) -> bool:
        return authorized.decision.decision is Decision.ALLOW

    @staticmethod
    def _target_uuid(command: ApplicationCommand) -> UUID:
        if command.target_id is None:
            raise MishkanError(ErrorCode.OUTPUT_CONTRACT, "command requires a target identity")
        return UUID(command.target_id)

    @staticmethod
    def _validate_envelope(command: ApplicationCommand) -> None:
        target_type, identity_mode = _COMMAND_TARGETS[command.command_type]
        if command.target_type != target_type:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "application command target type differs from its public contract",
                details={"command_type": command.command_type, "target_type": target_type},
            )
        if identity_mode == "absent" and command.target_id is not None:
            raise MishkanError(ErrorCode.OUTPUT_CONTRACT, "command target identity must be absent")
        if identity_mode in {"required", "uuid"} and command.target_id is None:
            raise MishkanError(ErrorCode.OUTPUT_CONTRACT, "command target identity is required")
        if identity_mode == "uuid" and command.target_id is not None:
            try:
                UUID(command.target_id)
            except ValueError as exc:
                raise MishkanError(
                    ErrorCode.OUTPUT_CONTRACT, "command target identity must be a UUID"
                ) from exc
        required, optional = _COMMAND_PAYLOAD_FIELDS[command.command_type]
        received = frozenset(command.payload)
        if not required.issubset(received) or not received.issubset(required | optional):
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "application command payload fields differ from its public contract",
                details={
                    "command_type": command.command_type,
                    "required_fields": sorted(required),
                    "optional_fields": sorted(optional),
                    "received_fields": sorted(received),
                },
            )

    @staticmethod
    def _change_scope(
        change_set: ChangeSet, base_effects: tuple[str, ...]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        paths = {
            value
            for operation in change_set.operations
            for value in (operation.path, operation.destination)
            if value is not None
        }
        return tuple(sorted(paths)), tuple(sorted({*base_effects, *change_set.declared_effects}))

    @staticmethod
    def _remaining_seconds(deadline: datetime) -> int:
        return max(1, math.ceil((deadline - utc_now()).total_seconds()))

    def _mcp_connection(self, command: ApplicationCommand) -> McpConnectionConfig:
        if command.payload or command.target_id is None:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "MCP connection command accepts only a configured target identity",
            )
        mcp: McpConfig | None = self._config.mcp
        if mcp is None:
            raise MishkanError(ErrorCode.MCP, "MCP mediation is not configured")
        connection = mcp.connections.get(command.target_id)
        if connection is None or not connection.enabled:
            raise MishkanError(ErrorCode.MCP, "MCP connection is not enabled")
        return connection

    def _configured_mcp_connection(self, connection_id: str) -> McpConnectionConfig:
        mcp = self._config.mcp
        if mcp is None:
            raise MishkanError(ErrorCode.MCP, "MCP mediation is not configured")
        connection = mcp.connections.get(connection_id)
        if connection is None or not connection.enabled:
            raise MishkanError(ErrorCode.MCP, "MCP connection is not enabled")
        return connection

    @staticmethod
    def _network_destination(endpoint: str) -> str:
        parsed = urlsplit(endpoint)
        assert parsed.hostname is not None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return f"{parsed.scheme}://{parsed.hostname}:{port}"
