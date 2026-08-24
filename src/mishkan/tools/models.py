"""Versioned atomic tool, toolset, registry, binding, and availability contracts."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.policy.models import ResourceRequest, security_identifier


class ToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EffectClass(StrEnum):
    READ = "read"
    FILESYSTEM_WRITE = "filesystem_write"
    COMMAND = "command"
    NETWORK = "network"
    REPOSITORY_WRITE = "repository_write"
    REPOSITORY_REMOTE_WRITE = "repository_remote_write"
    DEPLOYMENT = "deployment"
    RELEASE = "release"
    MIGRATION = "migration"
    REGISTRY_LIFECYCLE = "registry_lifecycle"


class Idempotency(StrEnum):
    IDEMPOTENT = "idempotent"
    DEDUPLICATION_KEY = "deduplication_key"
    COMPENSATABLE = "compensatable"
    NONE = "none"


class SourceKind(StrEnum):
    NATIVE = "native"
    PROJECT = "project"
    OPERATOR = "operator"
    MCP = "mcp"


class AvailabilityState(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class AvailabilityConditions(ToolModel):
    platforms: tuple[str, ...] = ("*",)
    runtimes: tuple[str, ...] = ("*",)
    credential_refs: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    min_memory_mb: int | None = Field(default=None, ge=1)


class ToolMetadata(ToolModel):
    tool_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    summary: str = Field(min_length=3, max_length=500)
    effect_class: EffectClass
    source_id: str = Field(min_length=1)
    source_kind: SourceKind
    contract_uri: str = Field(min_length=1)
    availability: AvailabilityConditions = Field(default_factory=AvailabilityConditions)

    @field_validator("tool_id", "source_id", "contract_uri")
    @classmethod
    def stable_identifiers(cls, value: str) -> str:
        return security_identifier(value)


class ToolContract(ToolModel):
    schema_version: str = "1.0"
    tool_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    crewai_name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    summary: str = Field(min_length=3, max_length=500)
    effect_class: EffectClass
    source_id: str = Field(min_length=1)
    source_kind: SourceKind
    adapter: str = Field(min_length=1)
    input_schema: dict[str, Any]
    result_schema: dict[str, Any]
    timeout_behavior: str = Field(min_length=1)
    idempotency: Idempotency
    target_scopes: tuple[str, ...] = Field(min_length=1)
    target_arguments: dict[str, tuple[str, ...]]
    credential_refs: tuple[str, ...] = ()
    availability: AvailabilityConditions = Field(default_factory=AvailabilityConditions)
    resources: ResourceRequest
    adapter_config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def schemas_are_objects(self) -> Self:
        if self.input_schema.get("type") != "object" or self.result_schema.get("type") != "object":
            raise ValueError("tool input and result schemas must be JSON object schemas")
        if set(self.target_arguments) != set(self.target_scopes):
            raise ValueError("tool target argument declarations must match its target scopes")
        if any(not selectors for selectors in self.target_arguments.values()):
            raise ValueError("each tool target scope must declare an argument selector")
        return self

    @property
    def provenance_fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"provenance_fingerprint"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @property
    def description(self) -> str:
        return self.summary

    @property
    def effect(self) -> str:
        return self.effect_class.value

    @property
    def max_bytes(self) -> int:
        value = self.adapter_config.get("max_bytes")
        if not isinstance(value, int) or value < 1:
            raise ValueError(f"tool {self.tool_id} does not declare a positive max_bytes")
        return value


class ToolsetDefinition(ToolModel):
    toolset_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    summary: str = Field(min_length=3)
    tools: tuple[str, ...] = ()
    toolsets: tuple[str, ...] = ()
    exclude_tools: tuple[str, ...] = ()
    max_depth: int = Field(default=8, ge=1, le=64)


class ToolSourceIndex(ToolModel):
    schema_version: str = "1.0"
    source_id: str = Field(min_length=1)
    source_kind: SourceKind
    revision: str = Field(min_length=1)
    adoption_authority: str = Field(min_length=1)
    tools: tuple[ToolMetadata, ...] = ()
    toolsets: tuple[ToolsetDefinition, ...] = ()

    @model_validator(mode="after")
    def entries_are_unique(self) -> Self:
        tool_ids = [tool.tool_id for tool in self.tools]
        toolset_ids = [toolset.toolset_id for toolset in self.toolsets]
        if len(tool_ids) != len(set(tool_ids)):
            raise ValueError("tool source contains duplicate tool identities")
        if len(toolset_ids) != len(set(toolset_ids)):
            raise ValueError("tool source contains duplicate toolset identities")
        if any(tool.source_id != self.source_id for tool in self.tools):
            raise ValueError("tool metadata source identity differs from its index")
        return self

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"fingerprint"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class AvailabilityResult(ToolModel):
    tool_id: str
    state: AvailabilityState
    missing_conditions: tuple[str, ...] = ()


class RegistrySnapshot(ToolModel):
    schema_version: str = "1.0"
    tools: tuple[ToolContract, ...] = Field(min_length=1)
    resolved_toolsets: tuple[tuple[str, tuple[str, ...]], ...] = ()
    source_fingerprints: tuple[tuple[str, str], ...] = Field(min_length=1)
    fingerprint: str = Field(min_length=64, max_length=64)

    def require(self, tool_id: str) -> ToolContract:
        matches = [tool for tool in self.tools if tool.tool_id == tool_id]
        if len(matches) != 1:
            raise ValueError(f"registry snapshot does not contain exactly one {tool_id!r}")
        return matches[0]


class ToolBinding(ToolModel):
    schema_version: str = "1.0"
    task_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    tool_id: str = Field(min_length=1)
    tool_version: str = Field(min_length=1)
    contract_fingerprint: str = Field(min_length=64, max_length=64)
    registry_fingerprint: str = Field(min_length=64, max_length=64)
    allowed_targets: tuple[str, ...]
