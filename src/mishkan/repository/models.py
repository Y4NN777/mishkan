"""Versioned repository or prospective-workspace discovery contracts."""

from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mishkan.domain.identity import new_id
from mishkan.domain.time import require_aware, utc_now


class RepositoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RepositoryBinding(RepositoryModel):
    context_kind: Literal["repository"] = "repository"
    repository_id: str = Field(min_length=12)
    root: Path
    base_revision: str = Field(min_length=7)
    remote_url: str | None = None
    working_tree_dirty: bool
    working_tree_fingerprint: str = Field(min_length=64, max_length=64)

    @property
    def context_id(self) -> str:
        return self.repository_id

    @property
    def context_revision(self) -> str:
        return self.base_revision


class ProspectiveWorkspaceBinding(RepositoryModel):
    context_kind: Literal["prospective_workspace"] = "prospective_workspace"
    workspace_id: str = Field(min_length=12, max_length=256)
    root: Path
    discovery_revision: str = Field(min_length=7, max_length=128)
    workspace_fingerprint: str = Field(min_length=64, max_length=64)
    repository_id: None = None
    base_revision: None = None

    @property
    def context_id(self) -> str:
        return self.workspace_id

    @property
    def context_revision(self) -> str:
        return self.discovery_revision


class DiscoveryFact(RepositoryModel):
    kind: str = Field(min_length=1)
    value: str = Field(min_length=1)
    citations: tuple[Path, ...] = Field(min_length=1)


class DiscoverySnapshot(RepositoryModel):
    schema_version: str = "1.0"
    binding: RepositoryBinding | ProspectiveWorkspaceBinding
    facts: tuple[DiscoveryFact, ...]
    unknowns: tuple[str, ...]
    fingerprint: str = Field(min_length=64, max_length=64)

    @property
    def cited_paths(self) -> frozenset[Path]:
        return frozenset(path for fact in self.facts for path in fact.citations)


class RepositoryEstablishment(RepositoryModel):
    schema_version: Literal["1.0"] = "1.0"
    establishment_id: UUID = Field(default_factory=new_id)
    run_id: str = Field(min_length=1, max_length=256)
    prospective_workspace: ProspectiveWorkspaceBinding
    repository: RepositoryBinding
    evidence_references: tuple[str, ...] = Field(min_length=1)
    established_by: str = Field(min_length=1, max_length=256)
    established_at: datetime = Field(default_factory=utc_now)

    @field_validator("established_at")
    @classmethod
    def established_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)
