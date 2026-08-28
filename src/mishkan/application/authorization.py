"""Server-derived authorization scopes for authoritative application commands.

Command semantics live here as public, versioned contracts. Operational grants do
not: they remain in the configured public policy documents.
"""

from __future__ import annotations

import base64
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
    CredentialReference,
    McpConfig,
    McpConnectionConfig,
    McpTransport,
    MishkanConfig,
)
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.edits import ChangeSet
from mishkan.edits.git import GitEffectMode, GitEffectRequest
from mishkan.execution import ExecutionRequest, ExecutionSession
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
    def status(self, session_id: UUID) -> ExecutionSession: ...


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
        "artifact.upload.abort": CommandSemantics(
            "application.artifact.upload", "artifact", ("artifact.abort",)
        ),
        "artifact.reference.update": CommandSemantics(
            "application.artifact.reference", "artifact", ("artifact.reference.update",)
        ),
        "artifact.collection.create": CommandSemantics(
            "application.artifact.collection", "artifact", ("artifact.collection.create",)
        ),
        "artifact.hold.set": CommandSemantics(
            "application.artifact.retention", "artifact", ("artifact.hold.set",)
        ),
        "artifact.hold.release": CommandSemantics(
            "application.artifact.retention", "artifact", ("artifact.hold.release",)
        ),
        "artifact.pin.set": CommandSemantics(
            "application.artifact.retention", "artifact", ("artifact.pin.set",)
        ),
        "artifact.pin.release": CommandSemantics(
            "application.artifact.retention", "artifact", ("artifact.pin.release",)
        ),
        "artifact.gc.plan": CommandSemantics(
            "application.artifact.gc", "artifact", ("artifact.gc.plan",)
        ),
        "artifact.gc.apply": CommandSemantics(
            "application.artifact.gc", "artifact", ("artifact.gc.apply",)
        ),
        "artifact.reconcile.plan": CommandSemantics(
            "application.artifact.reconcile", "artifact", ("artifact.reconcile.plan",)
        ),
        "artifact.reconcile.apply": CommandSemantics(
            "application.artifact.reconcile", "artifact", ("artifact.reconcile.apply",)
        ),
        "event.hold.create": CommandSemantics(
            "application.event.hold", "control", ("event.hold.create",)
        ),
        "event.hold.release": CommandSemantics(
            "application.event.hold", "control", ("event.hold.release",)
        ),
        "event.retention.plan": CommandSemantics(
            "application.event.retention", "control", ("event.retention.plan",)
        ),
        "event.retention.apply": CommandSemantics(
            "application.event.retention", "control", ("event.retention.apply",)
        ),
        "change.plan": CommandSemantics("application.change.plan", "filesystem", ("change.plan",)),
        "change.apply": CommandSemantics(
            "application.change.apply", "filesystem", ("change.apply",)
        ),
        "change.reconcile": CommandSemantics(
            "application.change.reconcile", "filesystem", ("change.reconcile",)
        ),
        **{
            f"git.{mode.value}": CommandSemantics(
                f"git.{mode.value}",
                mode.effect_class,
                (f"git.{mode.value}",),
                mode.value in {"push", "force_with_lease", "force_push"},
            )
            for mode in GitEffectMode
        },
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
        "artifact.upload.abort": ("artifact_upload", "uuid"),
        "artifact.reference.update": ("artifact_reference", "absent"),
        "artifact.collection.create": ("artifact_service", "absent"),
        "artifact.hold.set": ("artifact", "uuid"),
        "artifact.hold.release": ("artifact", "uuid"),
        "artifact.pin.set": ("artifact", "uuid"),
        "artifact.pin.release": ("artifact", "uuid"),
        "artifact.gc.plan": ("artifact_service", "absent"),
        "artifact.gc.apply": ("artifact_gc_plan", "uuid"),
        "artifact.reconcile.plan": ("artifact_service", "absent"),
        "artifact.reconcile.apply": ("artifact_reconciliation_plan", "uuid"),
        "event.hold.create": ("event_store", "absent"),
        "event.hold.release": ("event_hold", "uuid"),
        "event.retention.plan": ("event_store", "absent"),
        "event.retention.apply": ("event_retention_plan", "uuid"),
        "change.plan": ("change_set", "uuid"),
        "change.apply": ("change_set", "uuid"),
        "change.reconcile": ("change_set", "uuid"),
        **{f"git.{mode.value}": ("git_repository", "required") for mode in GitEffectMode},
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
        "artifact.upload.abort": (frozenset(), frozenset()),
        "artifact.reference.update": (
            frozenset({"scope", "name", "artifact_reference", "expected_reference_revision"}),
            frozenset(),
        ),
        "artifact.collection.create": (frozenset({"entries"}), frozenset()),
        "artifact.hold.set": (frozenset({"reason"}), frozenset()),
        "artifact.hold.release": (frozenset(), frozenset()),
        "artifact.pin.set": (frozenset(), frozenset()),
        "artifact.pin.release": (frozenset(), frozenset()),
        "artifact.gc.plan": (frozenset({"watermark"}), frozenset()),
        "artifact.gc.apply": (frozenset(), frozenset()),
        "artifact.reconcile.plan": (frozenset(), frozenset()),
        "artifact.reconcile.apply": (frozenset(), frozenset()),
        "event.hold.create": (
            frozenset({"scope", "reason"}),
            frozenset({"scope_id"}),
        ),
        "event.hold.release": (frozenset(), frozenset()),
        "event.retention.plan": (frozenset(), frozenset()),
        "event.retention.apply": (frozenset(), frozenset()),
        "change.plan": (frozenset({"change_set"}), frozenset()),
        "change.apply": (frozenset(), frozenset()),
        "change.reconcile": (frozenset(), frozenset()),
        **{f"git.{mode.value}": (frozenset({"request"}), frozenset()) for mode in GitEffectMode},
        "session.start": (frozenset({"request"}), frozenset()),
        "session.write": (
            frozenset({"content_base64", "declared_effects", "network_destinations"}),
            frozenset(),
        ),
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
    session_request: ExecutionRequest | None = None
    git_request: GitEffectRequest | None = None


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
        remotes: tuple[str, ...] = ()
        branches: tuple[str, ...] = ()
        uses_network = semantics.network
        effects = semantics.effects
        timeout = 120
        session_request: ExecutionRequest | None = None
        git_request: GitEffectRequest | None = None

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
            elif normalized.command_type.startswith("git."):
                git_request = GitEffectRequest.model_validate(normalized.payload["request"])
                if normalized.command_type != f"git.{git_request.mode.value}":
                    raise MishkanError(
                        ErrorCode.OUTPUT_CONTRACT,
                        "Git command type differs from its typed effect request",
                    )
                if git_request.workspace.resolve(strict=True) != self._workspace:
                    raise MishkanError(
                        ErrorCode.AUTHORITY_NOT_GRANTED,
                        "Git effect must target the daemon's exact configured repository",
                    )
                if normalized.target_id != str(self._workspace):
                    raise MishkanError(
                        ErrorCode.OUTPUT_CONTRACT,
                        "Git command target differs from the configured repository identity",
                    )
                if (
                    git_request.credential_reference is not None
                    and git_request.credential_reference not in self._config.credential_bindings
                ):
                    raise MishkanError(
                        ErrorCode.AUTHORIZATION_MISSING,
                        "Git credential reference is not configured",
                    )
                paths = git_request.paths
                remotes = (git_request.remote,) if git_request.remote else ()
                branches = (git_request.branch,) if git_request.branch else ()
                credentials = (
                    (git_request.credential_reference,)
                    if git_request.credential_reference is not None
                    else ()
                )
                timeout = git_request.timeout_seconds
            elif normalized.command_type == "session.start":
                session_request = ExecutionRequest.model_validate(normalized.payload["request"])
                if session_request.owner != normalized.actor_id:
                    raise MishkanError(
                        ErrorCode.AUTHORITY_NOT_GRANTED,
                        "session owner must match the authenticated command actor",
                    )
                paths = tuple(dict.fromkeys((session_request.cwd, *session_request.declared_paths)))
                assert session_request.executable is not None
                executables = (session_request.executable,)
                arguments = session_request.args
                environments = tuple(sorted(session_request.environment))
                credentials = tuple(
                    sorted(
                        {
                            *(
                                item.locator
                                for item in session_request.credential_environment.values()
                                if isinstance(item, CredentialReference)
                            ),
                            *(item.locator for item in session_request.credential_references),
                        }
                    )
                )
                effects = tuple(sorted({*effects, *session_request.declared_effects}))
                network_destinations = tuple(
                    self._network_destination(value)
                    for value in session_request.network_destinations
                )
                uses_network = bool(network_destinations)
                assert session_request.deadline is not None
                timeout = self._remaining_seconds(session_request.deadline)
            elif normalized.command_type.startswith("session."):
                record = self._sessions.status(self._target_uuid(normalized))
                if record.owner != normalized.actor_id:
                    raise MishkanError(
                        ErrorCode.AUTHORITY_NOT_GRANTED,
                        "session command actor does not own the target session",
                    )
                external_resources = (f"session:{record.session_id}",)
                if normalized.command_type == "session.write":
                    encoded = normalized.payload["content_base64"]
                    if not isinstance(encoded, str):
                        raise ValueError("session input must be base64 text")
                    content = base64.b64decode(encoded, validate=True)
                    sessions = self._config.sessions
                    assert sessions is not None
                    limit = sessions.profiles[record.profile].max_input_bytes
                    if len(content) > limit:
                        raise ValueError("session input exceeds its configured bound")
                    arguments = (content.decode("utf-8", errors="surrogateescape"),)
                    declared = self._string_tuple(normalized.payload["declared_effects"])
                    destinations = self._string_tuple(normalized.payload["network_destinations"])
                    effects = tuple(sorted({*effects, *declared}))
                    network_destinations = tuple(
                        self._network_destination(value) for value in destinations
                    )
                    uses_network = bool(network_destinations)
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
            remotes=remotes,
            branches=branches,
            environments=environments,
            credentials=credentials,
            external_resources=external_resources,
            resources=ResourceRequest(
                timeout_seconds=max(1, min(timeout, 86_400)),
                network=uses_network,
            ),
        )
        decision = PolicyAuthority().evaluate(request, self._policy)
        return AuthorizedApplicationCommand(
            normalized,
            request,
            decision,
            session_request=session_request,
            git_request=git_request,
        )

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

    @staticmethod
    def _string_tuple(value: object) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ValueError("application command value must be a list of non-empty strings")
        if len(value) != len(set(value)):
            raise ValueError("application command string values must be unique")
        return tuple(value)

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
        if not parsed.scheme or parsed.hostname is None:
            raise ValueError("network destination must be an absolute URL")
        port = parsed.port
        if port is None:
            if parsed.scheme == "https":
                port = 443
            elif parsed.scheme == "http":
                port = 80
            else:
                raise ValueError("non-HTTP network destination requires an explicit port")
        return f"{parsed.scheme}://{parsed.hostname}:{port}"
