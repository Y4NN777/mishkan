"""Confirmed portable engineer-profile facts, separate from local observations."""

from __future__ import annotations

from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.sources import resolve_source_path
from mishkan.domain.time import require_aware


class EngineerProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConfirmedEngineerFact(EngineerProfileModel):
    fact_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    category: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    value: str = Field(min_length=1, max_length=2_048)
    confirmed_by: str = Field(min_length=1, max_length=256)
    confirmed_at: datetime
    evidence_references: tuple[str, ...] = ()

    @field_validator("confirmed_at")
    @classmethod
    def confirmation_time_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)

    @field_validator("evidence_references")
    @classmethod
    def evidence_is_bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item or len(item) > 2_048 for item in value):
            raise ValueError("engineer-profile evidence references must be bounded and unique")
        return value


class EngineerProfile(EngineerProfileModel):
    schema_version: Literal["1.0"] = "1.0"
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    revision: str = Field(min_length=1, max_length=512)
    facts: tuple[ConfirmedEngineerFact, ...]

    @model_validator(mode="after")
    def facts_are_unique(self) -> EngineerProfile:
        identities = [fact.fact_id for fact in self.facts]
        if len(identities) != len(set(identities)):
            raise ValueError("engineer-profile fact identities must be unique")
        return self


class EngineerProfileLoader:
    def load(self, source: str, project_root: Path) -> EngineerProfile:
        try:
            if source.startswith("package://"):
                location = source.removeprefix("package://")
                package, separator, resource = location.rpartition("/")
                if not separator:
                    raise ValueError("package engineer-profile source is invalid")
                text = files(package).joinpath(resource).read_text(encoding="utf-8")
            else:
                path = resolve_source_path(source, project_root, "engineer profile")
                text = path.read_text(encoding="utf-8")
            return EngineerProfile.model_validate(yaml.safe_load(text))
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "confirmed engineer profile cannot be loaded",
                details={"source": source},
            ) from exc
