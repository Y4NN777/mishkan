"""Public contracts for portable procedural skills."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SkillModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SkillSourceKind(StrEnum):
    BUNDLED = "bundled"
    PROJECT = "project"
    OPERATOR = "operator"
    COMMUNITY = "community"
    SOURCE_CONTROL = "source_control"
    URL = "url"
    HUB = "hub"


class SkillTrustState(StrEnum):
    UNTRUSTED = "untrusted"
    TRUSTED = "trusted"
    QUARANTINED = "quarantined"


class SkillActivationState(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    INACTIVE = "inactive"
    QUARANTINED = "quarantined"
    ARCHIVED = "archived"


class SkillUseOutcome(StrEnum):
    HIT = "hit"
    PARTIAL = "partial"
    MISS = "miss"


class SkillBounds(SkillModel):
    max_frontmatter_bytes: int = Field(ge=128, le=1_048_576)
    max_manifest_bytes: int = Field(ge=256, le=16_777_216)
    max_resource_bytes: int = Field(ge=1, le=1_073_741_824)
    max_package_bytes: int = Field(ge=256, le=4_294_967_296)
    max_package_files: int = Field(ge=1, le=100_000)
    max_resource_depth: int = Field(ge=1, le=64)
    allow_frontmatter_extensions: bool


class SkillSourceDefinition(SkillModel):
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    kind: SkillSourceKind
    uri: str = Field(min_length=1, max_length=4_096)
    revision: str = Field(min_length=1, max_length=512)
    precedence: int = Field(ge=-1_000_000, le=1_000_000)
    trust: SkillTrustState
    default_activation: SkillActivationState
    package_activation: dict[str, SkillActivationState] = Field(default_factory=dict)
    enabled: bool = True


class SkillMetadata(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    description: str = Field(min_length=1, max_length=1_024)
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
    source_id: str = Field(min_length=1, max_length=128)
    source_kind: SkillSourceKind
    source_revision: str = Field(min_length=1, max_length=512)
    package_uri: str = Field(min_length=1, max_length=4_096)
    package_fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    trust: SkillTrustState
    activation: SkillActivationState
    license: str | None = Field(default=None, min_length=1, max_length=512)
    compatibility_summary: str | None = Field(default=None, min_length=1, max_length=500)
    allowed_tools_hint: str | None = Field(default=None, min_length=1, max_length=2_048)
    author_claim: str | None = Field(default=None, min_length=1, max_length=512)
    platforms: tuple[str, ...] = ("*",)
    required_tools: tuple[str, ...] = ()
    fallback_tools: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    organization_versions: tuple[str, ...] = ("*",)
    task_classes: tuple[str, ...] = ()
    resource_paths: tuple[str, ...] = ()

    @model_validator(mode="after")
    def fallbacks_belong_to_requirements(self) -> SkillMetadata:
        if not set(self.fallback_tools).issubset(self.required_tools):
            raise ValueError("skill fallbacks must correspond to required tools")
        return self


class SkillSelectionContext(SkillModel):
    task_id: str = Field(min_length=1, max_length=256)
    task_class: str = Field(min_length=1, max_length=256)
    consuming_identity: str = Field(min_length=1, max_length=256)
    platform: str = Field(min_length=1, max_length=128)
    organization_version: str = Field(min_length=1, max_length=512)
    available_tools: frozenset[str] = frozenset()


class SkillSelection(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    requested_name: str = Field(min_length=1, max_length=64)
    context: SkillSelectionContext
    outcome: SkillUseOutcome
    selected: SkillMetadata | None = None
    reason: str = Field(min_length=1, max_length=2_048)
    missing_conditions: tuple[str, ...] = ()
    fallback_bindings: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def result_matches_outcome(self) -> SkillSelection:
        if self.outcome is SkillUseOutcome.MISS and self.selected is not None:
            raise ValueError("skill miss cannot contain a selection")
        if self.outcome is not SkillUseOutcome.MISS and self.selected is None:
            raise ValueError("skill hit or partial outcome requires a selection")
        return self


class SkillLoadedResource(SkillModel):
    path: str = Field(min_length=1, max_length=512)
    fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)


class SkillLoadEvidence(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=1, max_length=256)
    task_class: str = Field(min_length=1, max_length=256)
    consuming_identity: str = Field(min_length=1, max_length=256)
    skill_name: str = Field(min_length=1, max_length=64)
    skill_version: str = Field(min_length=1, max_length=128)
    package_fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    outcome: SkillUseOutcome
    reason: str = Field(min_length=1, max_length=2_048)
    instruction_fingerprint: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    loaded_resources: tuple[SkillLoadedResource, ...] = ()
