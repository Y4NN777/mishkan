"""Versioned repository binding and discovery contracts."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class RepositoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RepositoryBinding(RepositoryModel):
    repository_id: str = Field(min_length=12)
    root: Path
    base_revision: str = Field(min_length=7)
    remote_url: str | None = None


class DiscoveryFact(RepositoryModel):
    kind: str = Field(min_length=1)
    value: str = Field(min_length=1)
    citations: tuple[Path, ...] = Field(min_length=1)


class DiscoverySnapshot(RepositoryModel):
    schema_version: str = "1.0"
    binding: RepositoryBinding
    facts: tuple[DiscoveryFact, ...]
    unknowns: tuple[str, ...]
    fingerprint: str = Field(min_length=64, max_length=64)

    @property
    def cited_paths(self) -> frozenset[Path]:
        return frozenset(path for fact in self.facts for path in fact.citations)
