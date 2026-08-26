"""Strict public configuration contracts."""

import ipaddress
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.domain.time import validate_timezone


class StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OperatingMode(StrEnum):
    LOCAL = "local"
    CLOUD = "cloud"
    HYBRID = "hybrid"
    DISTRIBUTED = "distributed"


class CredentialSource(StrEnum):
    ENV = "env"
    FILE = "file"
    KEYRING = "keyring"
    COMMAND = "command"


class CredentialReference(StrictConfigModel):
    """A late-resolution locator; never a credential value."""

    source: CredentialSource
    locator: str = Field(min_length=1)


class ProviderConfig(StrictConfigModel):
    kind: str = Field(min_length=1)
    endpoint: AnyUrl
    credential_pool: tuple[CredentialReference, ...] = ()
    probe_url: AnyUrl | None = None


class ServiceConfig(StrictConfigModel):
    kind: str = Field(min_length=1)
    endpoint: AnyUrl
    credential_refs: tuple[CredentialReference, ...] = ()
    required: bool = False
    probe_url: AnyUrl | None = None


class ModelCandidate(StrictConfigModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)


class ModelRoute(StrictConfigModel):
    candidates: tuple[ModelCandidate, ...] = Field(min_length=1)


class ProjectConfig(StrictConfigModel):
    workspace: Path


class CrewAIRuntimeConfig(StrictConfigModel):
    tracing: bool = False
    telemetry: bool = False
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    model_timeout_seconds: int = Field(default=120, ge=10, le=3_600)
    model_transport_retries: int = Field(default=0, ge=0, le=10)
    model_max_output_tokens: int = Field(default=2_048, ge=128, le=131_072)
    max_agent_iterations: int = Field(default=4, ge=1, le=100)
    task_execution_retries: int = Field(default=2, ge=0, le=10)
    plan_validation_retries: int = Field(default=2, ge=0, le=10)
    review_retries: int = Field(default=2, ge=0, le=10)
    structured_output_retries: int = Field(default=2, ge=0, le=10)


class DaemonConfig(StrictConfigModel):
    host: str
    port: int = Field(ge=1, le=65_535)
    token_file: Path
    heartbeat_seconds: int = Field(ge=1, le=300)
    event_poll_seconds: float = Field(ge=0.05, le=30)
    event_page_limit: int = Field(ge=1, le=1_000)
    request_timeout_seconds: int = Field(ge=1, le=3_600)

    @field_validator("host")
    @classmethod
    def host_is_loopback_for_i03(cls, value: str) -> str:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("I03 daemon host must be a literal loopback address") from exc
        if not address.is_loopback:
            raise ValueError("I03 daemon host must be loopback-only")
        return value

    @field_validator("token_file")
    @classmethod
    def token_file_is_project_relative(cls, value: Path) -> Path:
        if value.is_absolute() or not value.parts:
            raise ValueError("daemon token file must be project-relative")
        return value


class PersistenceConfig(StrictConfigModel):
    database: Path
    busy_timeout_ms: int = Field(ge=1, le=300_000)
    event_retention_days: int = Field(ge=1, le=36_500)

    @field_validator("database")
    @classmethod
    def database_is_project_relative(cls, value: Path) -> Path:
        if value.is_absolute() or not value.parts:
            raise ValueError("metadata database must be project-relative")
        return value


class ArtifactConfig(StrictConfigModel):
    root: Path
    max_artifact_bytes: int = Field(ge=1)
    chunk_bytes: int = Field(ge=1)
    staging_ttl_seconds: int = Field(ge=1)

    @field_validator("root")
    @classmethod
    def root_is_project_relative(cls, value: Path) -> Path:
        if value.is_absolute() or not value.parts:
            raise ValueError("artifact root must be project-relative")
        return value


class MishkanConfig(StrictConfigModel):
    """Complete effective configuration required before a run can be accepted."""

    schema_version: str
    mode: OperatingMode
    timezone: str
    project: ProjectConfig
    providers: dict[str, ProviderConfig] = Field(min_length=1)
    model_routes: dict[str, ModelRoute] = Field(min_length=1)
    agent_routes: dict[str, str] = Field(default_factory=dict)
    services: dict[str, ServiceConfig] = Field(default_factory=dict)
    policy_sources: tuple[str, ...] = Field(min_length=1)
    tool_sources: tuple[str, ...] = ()
    inspection_profile: str | None = None
    isolation_profiles: tuple[str, ...] = ()
    crewai: CrewAIRuntimeConfig = Field(default_factory=CrewAIRuntimeConfig)
    daemon: DaemonConfig | None = None
    persistence: PersistenceConfig | None = None
    artifacts: ArtifactConfig | None = None

    @field_validator("timezone")
    @classmethod
    def timezone_is_iana(cls, value: str) -> str:
        return validate_timezone(value)

    @model_validator(mode="after")
    def references_exist(self) -> Self:
        if self.schema_version == "1.1":
            missing = [
                field
                for field, value in (
                    ("tool_sources", self.tool_sources),
                    ("inspection_profile", self.inspection_profile),
                    ("isolation_profiles", self.isolation_profiles),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"configuration 1.1 requires governed capability fields: {missing}"
                )
        if self.schema_version == "1.2":
            missing_i03 = [
                field
                for field, value in (
                    ("daemon", self.daemon),
                    ("persistence", self.persistence),
                    ("artifacts", self.artifacts),
                )
                if value is None
            ]
            if missing_i03:
                raise ValueError(f"configuration 1.2 requires I03 fields: {missing_i03}")

        missing_providers = sorted(
            {
                candidate.provider
                for route in self.model_routes.values()
                for candidate in route.candidates
                if candidate.provider not in self.providers
            }
        )
        if missing_providers:
            raise ValueError(f"model routes reference unknown providers: {missing_providers}")

        missing_routes = sorted(
            {route for route in self.agent_routes.values() if route not in self.model_routes}
        )
        if missing_routes:
            raise ValueError(f"agent overrides reference unknown routes: {missing_routes}")
        return self
