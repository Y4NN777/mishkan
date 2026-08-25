"""Frozen public contracts for policy, authorization, approval, and revocation."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.domain.identity import DomainRecord
from mishkan.domain.time import require_aware, utc_now


def canonical_fingerprint(value: BaseModel | Mapping[str, object]) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def security_identifier(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    if normalized != value or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise ValueError("security identifiers must use stable visible Unicode")
    return value


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Decision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class ResourceRequest(PolicyModel):
    timeout_seconds: int = Field(ge=1, le=86_400)
    memory_mb: int | None = Field(default=None, ge=1)
    network: bool = False
    concurrency: int = Field(default=1, ge=1)


class PolicyScope(PolicyModel):
    identities: tuple[str, ...] = ("*",)
    objective_classes: tuple[str, ...] = ("*",)
    repositories: tuple[str, ...] = ("*",)
    outcomes: tuple[str, ...] = ("*",)
    roles: tuple[str, ...] = ("*",)
    capabilities: tuple[str, ...] = ("*",)
    effect_classes: tuple[str, ...] = ("*",)
    paths: tuple[str, ...] = ("*",)
    executables: tuple[str, ...] = ("*",)
    arguments: tuple[str, ...] = ("*",)
    network_destinations: tuple[str, ...] = ("*",)
    remotes: tuple[str, ...] = ("*",)
    branches: tuple[str, ...] = ("*",)
    environments: tuple[str, ...] = ("*",)
    credentials: tuple[str, ...] = ("*",)
    external_resources: tuple[str, ...] = ("*",)
    isolation_profiles: tuple[str, ...] = ("*",)
    max_timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    max_memory_mb: int | None = Field(default=None, ge=1)
    allow_network: bool | None = None
    max_concurrency: int | None = Field(default=None, ge=1)

    @field_validator(
        "identities",
        "objective_classes",
        "repositories",
        "outcomes",
        "roles",
        "capabilities",
        "effect_classes",
        "paths",
        "executables",
        "network_destinations",
        "remotes",
        "branches",
        "environments",
        "credentials",
        "external_resources",
        "isolation_profiles",
    )
    @classmethod
    def selectors_are_unambiguous(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("policy selectors must be non-empty and unique")
        return tuple(security_identifier(item) for item in value)

    @field_validator("arguments")
    @classmethod
    def argument_selectors_are_literal(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)) or any("\x00" in item for item in value):
            raise ValueError("policy argument selectors must be non-empty, unique, and NUL-free")
        return value


class PolicyRule(PolicyModel):
    rule_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    priority: int = 0
    decision: Decision
    scope: PolicyScope


class PolicyDocument(PolicyModel):
    schema_version: str = "1.0"
    source_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    adoption_authority: str = Field(min_length=1)
    priority: int = 0
    effective_from: datetime | None = None
    retired_at: datetime | None = None
    rules: tuple[PolicyRule, ...] = Field(min_length=1)

    @field_validator("source_id", "revision", "adoption_authority")
    @classmethod
    def identifiers_are_unambiguous(cls, value: str) -> str:
        return security_identifier(value)

    @field_validator("effective_from", "retired_at")
    @classmethod
    def times_are_aware(cls, value: datetime | None) -> datetime | None:
        return require_aware(value) if value is not None else None

    @model_validator(mode="after")
    def rule_ids_are_unique(self) -> Self:
        identifiers = [rule.rule_id for rule in self.rules]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("policy rule identifiers must be unique within a source")
        if self.effective_from and self.retired_at and self.effective_from >= self.retired_at:
            raise ValueError("policy retirement must occur after activation")
        return self

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self)


class EffectivePolicy(PolicyModel):
    schema_version: str = "1.0"
    documents: tuple[PolicyDocument, ...] = Field(min_length=1)
    source_uris: tuple[str, ...] = Field(min_length=1)
    fingerprint: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def sources_align(self) -> Self:
        if len(self.documents) != len(self.source_uris):
            raise ValueError("each policy document must retain its configured source URI")
        return self


class AuthorizationRequest(PolicyModel):
    schema_version: str = "1.0"
    plan_fingerprint: str = Field(min_length=64, max_length=64)
    identity: str = Field(min_length=1)
    objective_class: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    role: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    effect_class: str = Field(min_length=1)
    paths: tuple[str, ...] = ()
    executables: tuple[str, ...] = ()
    arguments: tuple[str, ...] = ()
    network_destinations: tuple[str, ...] = ()
    remotes: tuple[str, ...] = ()
    branches: tuple[str, ...] = ()
    environments: tuple[str, ...] = ()
    credentials: tuple[str, ...] = ()
    external_resources: tuple[str, ...] = ()
    isolation_profile: str | None = None
    resources: ResourceRequest

    @field_validator(
        "identity",
        "objective_class",
        "repository",
        "outcome",
        "role",
        "capability",
        "effect_class",
    )
    @classmethod
    def required_identifiers_are_unambiguous(cls, value: str) -> str:
        return security_identifier(value)

    @field_validator(
        "paths",
        "executables",
        "network_destinations",
        "remotes",
        "branches",
        "environments",
        "credentials",
        "external_resources",
    )
    @classmethod
    def scoped_identifiers_are_unambiguous(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("authorization scopes must be unique")
        return tuple(security_identifier(item) for item in value)

    @field_validator("arguments")
    @classmethod
    def arguments_are_literal(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any("\x00" in item for item in value):
            raise ValueError("authorization arguments must be NUL-free")
        return value

    @field_validator("isolation_profile")
    @classmethod
    def isolation_identifier_is_unambiguous(cls, value: str | None) -> str | None:
        return security_identifier(value) if value is not None else None

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self)


class ApprovalEvidence(DomainRecord):
    request_fingerprint: str = Field(min_length=64, max_length=64)
    plan_fingerprint: str = Field(min_length=64, max_length=64)
    policy_fingerprint: str = Field(min_length=64, max_length=64)
    approved_by: str = Field(min_length=1)
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    reason: str = Field(min_length=1)

    @field_validator("expires_at", "revoked_at")
    @classmethod
    def approval_times_are_aware(cls, value: datetime | None) -> datetime | None:
        return require_aware(value) if value is not None else None

    def is_active_for(self, request: AuthorizationRequest, policy: EffectivePolicy) -> bool:
        now = utc_now()
        return (
            self.request_fingerprint == request.fingerprint
            and self.plan_fingerprint == request.plan_fingerprint
            and self.policy_fingerprint == policy.fingerprint
            and self.revoked_at is None
            and (self.expires_at is None or self.expires_at > now)
        )


class AuthorizationDecision(DomainRecord):
    request_fingerprint: str = Field(min_length=64, max_length=64)
    plan_fingerprint: str = Field(min_length=64, max_length=64)
    policy_fingerprint: str = Field(min_length=64, max_length=64)
    policy_revisions: tuple[str, ...] = Field(min_length=1)
    decision: Decision
    matched_rule_ids: tuple[str, ...]
    matched_scope: PolicyScope | None = None
    decided_by: str = Field(min_length=1)
    approval_id: str | None = None
    reason: str = Field(min_length=1)
