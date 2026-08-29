"""Versioned public engineering-environment reconnaissance profile."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.schema import SchemaRegistry
from mishkan.domain.sources import resolve_source_path


class EnvironmentProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EngineProbeDefinition(EnvironmentProfileModel):
    engine_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    executable_names: tuple[str, ...] = Field(min_length=1)
    adapter_id: str | None = Field(default=None, min_length=1, max_length=256)
    semantics: tuple[str, ...] = Field(min_length=1)
    platforms: tuple[str, ...] = ("*",)
    project_markers: tuple[str, ...] = ()
    safe_version_arguments: tuple[str, ...] = ("--version",)


class EnvironmentProfile(EnvironmentProfileModel):
    schema_version: str
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    revision: str = Field(min_length=1, max_length=512)
    descriptor_paths: dict[str, tuple[str, ...]] = Field(min_length=1)
    manifest_names: dict[str, tuple[str, ...]] = Field(min_length=1)
    engines: tuple[EngineProbeDefinition, ...] = Field(min_length=1)
    resolution_order: tuple[str, ...] = Field(min_length=1)
    max_observed_files: int = Field(ge=1, le=100_000)
    max_descriptor_bytes: int = Field(ge=1, le=1_073_741_824)
    max_manifest_depth: int = Field(ge=1, le=64)
    excluded_directories: tuple[str, ...] = Field(min_length=1)
    freshness_seconds: int = Field(ge=1, le=31_536_000)

    @model_validator(mode="after")
    def identities_are_unique(self) -> EnvironmentProfile:
        engine_ids = [engine.engine_id for engine in self.engines]
        if len(engine_ids) != len(set(engine_ids)):
            raise ValueError("environment engine identities must be unique")
        allowed_order = {
            "explicit_configuration",
            "project_declaration",
            "execution_environment",
            "active_pack",
            "isolated_materialization",
            "external_capability",
            "compatible_fallback",
        }
        if len(self.resolution_order) != len(set(self.resolution_order)) or (
            set(self.resolution_order) - allowed_order
        ):
            raise ValueError("environment resolution order is invalid")
        for name in self.excluded_directories:
            if not name or "/" in name or name in {".", ".."}:
                raise ValueError("environment excluded directories must be simple names")
        return self


def load_environment_profile(source: str, project_root: Path) -> EnvironmentProfile:
    try:
        if source.startswith("package://"):
            location = source.removeprefix("package://")
            package, separator, resource_name = location.rpartition("/")
            if not separator or not package or not resource_name:
                raise ValueError("package resource is invalid")
            text = files(package).joinpath(resource_name).read_text(encoding="utf-8")
        else:
            path = resolve_source_path(source, project_root, "environment profile")
            text = path.read_text(encoding="utf-8")
        document = yaml.safe_load(text)
        if not isinstance(document, dict):
            raise ValueError("environment profile must be a mapping")
        SchemaRegistry.require_supported(
            "mishkan.environment-profile",
            document.get("schema_version"),
        )
        return EnvironmentProfile.model_validate(document)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise MishkanError(
            ErrorCode.CONFIGURATION,
            "environment profile cannot be loaded",
            details={"source": source},
        ) from exc
