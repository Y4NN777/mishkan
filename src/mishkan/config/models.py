"""Strict public configuration contracts."""

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
    max_agent_iterations: int = Field(default=4, ge=1, le=100)
    plan_validation_retries: int = Field(default=2, ge=0, le=10)
    review_retries: int = Field(default=2, ge=0, le=10)
    structured_output_retries: int = Field(default=2, ge=0, le=10)


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
    crewai: CrewAIRuntimeConfig = Field(default_factory=CrewAIRuntimeConfig)

    @field_validator("timezone")
    @classmethod
    def timezone_is_iana(cls, value: str) -> str:
        return validate_timezone(value)

    @model_validator(mode="after")
    def references_exist(self) -> Self:
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
