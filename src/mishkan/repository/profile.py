"""Public, versioned repository discovery profile."""

from importlib.resources import files
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from mishkan.domain.schema import SchemaRegistry


class DiscoveryProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    manifests: dict[str, tuple[str, ...]] = Field(min_length=1)
    languages: dict[str, str] = Field(min_length=1)
    excluded_directories: tuple[str, ...] = ()
    evidence_limit: int = Field(default=200, ge=1, le=10_000)


def load_discovery_profile(source: Path | None = None) -> DiscoveryProfile:
    if source is None:
        resource = files("mishkan.resources.discovery").joinpath("default.yaml")
        document = yaml.safe_load(resource.read_text(encoding="utf-8"))
    else:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    profile = DiscoveryProfile.model_validate(document)
    SchemaRegistry.require_supported("mishkan.discovery", profile.schema_version)
    return profile
