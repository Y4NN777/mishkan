"""Versioned durable contracts for MCP connectivity and mediation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.config.models import McpProtocolStrategy, McpTransport
from mishkan.domain.identity import new_id
from mishkan.domain.time import require_aware, utc_now


class McpModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class McpDirection(StrEnum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class McpSessionState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    NEGOTIATING = "negotiating"
    READY = "ready"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    CANCELLING = "cancelling"
    CLOSED = "closed"
    LOST = "lost"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


class McpPrimitiveKind(StrEnum):
    TOOL = "tool"
    RESOURCE = "resource"
    PROMPT = "prompt"


class McpCallState(StrEnum):
    RESERVED = "reserved"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    LOST = "lost"
    UNCERTAIN = "uncertain"


class McpEffectDisposition(StrEnum):
    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"
    UNKNOWN = "unknown"


class McpRemoteTaskTerminal(StrEnum):
    IMMEDIATE = "immediate"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class McpConnectionRecord(McpModel):
    schema_version: str = "1.0"
    id: UUID = Field(default_factory=new_id)
    connection_id: str = Field(min_length=1)
    direction: McpDirection = McpDirection.OUTBOUND
    transport: McpTransport
    protocol_strategy: McpProtocolStrategy
    configured_protocol_versions: tuple[str, ...] = Field(min_length=1)
    negotiated_protocol_version: str | None = None
    trust: str = Field(min_length=1)
    exposure_profile: str = Field(min_length=1)
    remote_tasks_enabled: bool = False
    server_identity: str = Field(default="legacy:unknown", min_length=1)
    credential_references: tuple[str, ...] = ()
    credential_principal: str | None = None
    policy_fingerprint: str = Field(default="legacy:unknown", min_length=1)
    state: McpSessionState
    revision: int = Field(ge=0)
    schema_fingerprint: str | None = None
    task_tool_calls_supported: bool = False
    task_cancellation_supported: bool = False
    health: str = Field(min_length=1)
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def times_are_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def negotiated_version_was_configured(self) -> Self:
        if (
            self.negotiated_protocol_version is not None
            and self.negotiated_protocol_version not in self.configured_protocol_versions
        ):
            raise ValueError("negotiated MCP protocol version was not configured")
        return self


class McpPrimitiveDescriptor(McpModel):
    schema_version: str = "1.0"
    id: UUID = Field(default_factory=new_id)
    connection_id: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)
    kind: McpPrimitiveKind
    name: str = Field(min_length=1)
    title: str | None = None
    description: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] = Field(default_factory=dict)
    effect_disposition: McpEffectDisposition = McpEffectDisposition.UNKNOWN
    sensitivity: str = "untrusted"
    schema_hash: str
    provenance: str = Field(min_length=1)
    discovered_at: datetime = Field(default_factory=utc_now)

    @field_validator("discovered_at")
    @classmethod
    def discovery_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def hash_matches_claims(self) -> Self:
        if self.schema_hash != self.claim_hash(
            self.kind,
            self.name,
            self.input_schema,
            self.output_schema,
            self.annotations,
        ):
            raise ValueError("MCP primitive schema hash differs from normalized claims")
        return self

    @staticmethod
    def claim_hash(
        kind: McpPrimitiveKind,
        name: str,
        input_schema: dict[str, Any] | None,
        output_schema: dict[str, Any] | None,
        annotations: dict[str, Any],
    ) -> str:
        payload = json.dumps(
            {
                "kind": kind.value,
                "name": name,
                "input_schema": input_schema,
                "output_schema": output_schema,
                "annotations": annotations,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()


class McpDiscoverySnapshot(McpModel):
    schema_version: str = "1.0"
    connection_id: str
    protocol_version: str
    primitives: tuple[McpPrimitiveDescriptor, ...]
    schema_fingerprint: str
    task_tool_calls_supported: bool = False
    task_cancellation_supported: bool = False
    discovered_at: datetime = Field(default_factory=utc_now)

    @field_validator("discovered_at")
    @classmethod
    def snapshot_time_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def fingerprint_matches_primitives(self) -> Self:
        expected = self.claim_fingerprint(
            self.primitives,
            task_tool_calls_supported=self.task_tool_calls_supported,
            task_cancellation_supported=self.task_cancellation_supported,
        )
        if self.schema_fingerprint != expected:
            raise ValueError("MCP discovery fingerprint differs from normalized primitives")
        return self

    @staticmethod
    def claim_fingerprint(
        primitives: tuple[McpPrimitiveDescriptor, ...],
        *,
        task_tool_calls_supported: bool = False,
        task_cancellation_supported: bool = False,
    ) -> str:
        normalized = [
            {
                "kind": item.kind.value,
                "name": item.name,
                "schema_hash": item.schema_hash,
            }
            for item in sorted(primitives, key=lambda value: (value.kind, value.name))
        ]
        claims: object = normalized
        if task_tool_calls_supported or task_cancellation_supported:
            claims = {
                "primitives": normalized,
                "task_tool_calls_supported": task_tool_calls_supported,
                "task_cancellation_supported": task_cancellation_supported,
            }
        return hashlib.sha256(
            json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class McpClientCallOutcome(McpModel):
    output: dict[str, Any] | None = None
    remote_task_id: str | None = None
    terminal: McpRemoteTaskTerminal
    reason: str


class McpCallRequest(McpModel):
    schema_version: str = "1.0"
    id: UUID = Field(default_factory=new_id)
    connection_id: str = Field(min_length=1)
    primitive_name: str = Field(min_length=1)
    caller_identity: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    task_attempt_id: str = Field(min_length=1)
    arguments: dict[str, Any]
    declared_effects: tuple[str, ...]
    effect_disposition: McpEffectDisposition
    expected_schema_hash: str = Field(min_length=1)
    idempotency_key: UUID = Field(default_factory=new_id)
    remote_task_allowed: bool = False
    deadline: datetime

    @field_validator("deadline")
    @classmethod
    def deadline_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class McpCallResult(McpModel):
    schema_version: str = "1.0"
    request_id: UUID
    connection_id: str
    primitive_name: str
    state: McpCallState
    output: dict[str, Any] | None = None
    content_artifact_references: tuple[str, ...] = ()
    remote_task_id: str | None = None
    schema_hash: str
    error_code: str | None = None
    reason: str
    completed_at: datetime = Field(default_factory=utc_now)

    @field_validator("completed_at")
    @classmethod
    def completion_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class McpProgressEvent(McpModel):
    schema_version: str = "1.0"
    id: UUID = Field(default_factory=new_id)
    request_id: UUID
    cursor: int = Field(ge=0)
    progress: float | None = Field(default=None, ge=0)
    total: float | None = Field(default=None, gt=0)
    message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def progress_time_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)
