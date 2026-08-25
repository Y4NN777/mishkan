"""Versioned request and result contracts shared by execution capability modes."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.domain.time import require_aware

_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionMode(StrEnum):
    PROCESS = "process"
    SHELL = "shell"
    TERMINAL = "terminal"
    JOB = "job"


class ExecutionStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    LOST = "lost"
    UNCERTAIN = "uncertain"


class EffectSettlement(StrEnum):
    ABSENT = "absent"
    COMPLETED = "completed"
    UNCERTAIN = "uncertain"


class OutputPolicy(ExecutionModel):
    preview_bytes: int = Field(ge=1)


class ExecutionRequest(ExecutionModel):
    """Common execution envelope; mode-specific public operations constrain its fields."""

    schema_version: str = "1.0"
    execution_id: UUID = Field(default_factory=uuid4)
    mode: ExecutionMode
    cwd: str = Field(min_length=1)
    executable: str | None = None
    args: tuple[str, ...] = ()
    script: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    credential_environment: dict[str, str] = Field(default_factory=dict)
    stdin: str | None = None
    timeout_seconds: int = Field(ge=1, le=86_400)
    expected_exit_codes: tuple[int, ...] = (0,)
    declared_effects: tuple[str, ...] = ()
    output_policy: OutputPolicy

    @field_validator("cwd")
    @classmethod
    def cwd_is_relative(cls, value: str) -> str:
        if Path(value).is_absolute():
            raise ValueError("execution cwd must be workspace-relative")
        return value

    @field_validator("environment", "credential_environment")
    @classmethod
    def environment_names_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not _ENVIRONMENT_NAME.fullmatch(name) for name in value):
            raise ValueError("execution environment names must be portable identifiers")
        return value

    @field_validator("expected_exit_codes", "declared_effects")
    @classmethod
    def tuple_values_are_unique(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        if len(value) != len(set(value)):
            raise ValueError("execution tuple values must be unique")
        return value

    @model_validator(mode="after")
    def mode_fields_are_consistent(self) -> Self:
        if self.mode is ExecutionMode.PROCESS:
            if self.executable is None or not Path(self.executable).is_absolute():
                raise ValueError("direct process mode requires an absolute executable")
            if self.script is not None:
                raise ValueError("direct process mode does not accept a shell script")
        if set(self.environment) & set(self.credential_environment):
            raise ValueError("plain and credential environment names must not overlap")
        return self


class ExecutionResult(ExecutionModel):
    schema_version: str = "1.0"
    execution_id: UUID
    mode: ExecutionMode
    status: ExecutionStatus
    executable: str
    args: tuple[str, ...]
    cwd: str
    exit_code: int | None
    signal: int | None
    started_at: datetime
    finished_at: datetime
    stdout_preview: str
    stderr_preview: str
    stdout_bytes: int = Field(ge=0)
    stderr_bytes: int = Field(ge=0)
    stdout_digest: str
    stderr_digest: str
    stdout_artifact_ref: str | None = None
    stderr_artifact_ref: str | None = None
    truncated: bool
    termination_cause: str | None = None
    expected_exit_codes: tuple[int, ...]
    environment_names: tuple[str, ...]
    credential_environment_names: tuple[str, ...]
    declared_effects: tuple[str, ...]
    observed_effects: tuple[str, ...] = ()
    effect_settlement: EffectSettlement
    retryable: bool = False
    execution_location: str
    error: str | None = None

    @field_validator("started_at", "finished_at")
    @classmethod
    def times_are_aware(cls, value: datetime) -> datetime:
        return require_aware(value)
