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


class ShellDialect(StrEnum):
    BASH = "bash"


class ShellOptions(ExecutionModel):
    pipefail: bool = True
    errexit: bool = False
    nounset: bool = False
    inherit_errexit: bool = False


class ShellProfile(ExecutionModel):
    schema_version: str = "1.0"
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    revision: str = Field(min_length=1)
    dialect: ShellDialect = ShellDialect.BASH
    interpreter: str
    startup_files: tuple[str, ...] = ()
    options: ShellOptions = Field(default_factory=ShellOptions)

    @field_validator("interpreter")
    @classmethod
    def interpreter_is_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("shell profile interpreter must be absolute")
        return value

    @field_validator("startup_files")
    @classmethod
    def startup_files_are_relative_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("shell profile startup files must be unique")
        if any(not item or Path(item).is_absolute() for item in value):
            raise ValueError("shell profile startup files must be workspace-relative")
        return value


class OutputPolicy(ExecutionModel):
    preview_bytes: int = Field(ge=1)
    preserve_full_output_as_artifact: bool = False


class ExecutionRequest(ExecutionModel):
    """Common execution envelope; mode-specific public operations constrain its fields."""

    schema_version: str = "1.0"
    execution_id: UUID = Field(default_factory=uuid4)
    mode: ExecutionMode
    cwd: str = Field(min_length=1)
    executable: str | None = None
    args: tuple[str, ...] = ()
    script: str | None = Field(default=None, min_length=1)
    shell_profile: ShellProfile | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    credential_environment: dict[str, str] = Field(default_factory=dict)
    stdin: str | None = None
    timeout_seconds: int = Field(ge=1, le=86_400)
    expected_exit_codes: tuple[int, ...] = (0,)
    declared_paths: tuple[str, ...] = ()
    declared_executables: tuple[str, ...] = ()
    network_destinations: tuple[str, ...] = ()
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

    @field_validator(
        "expected_exit_codes",
        "declared_paths",
        "declared_executables",
        "network_destinations",
        "declared_effects",
    )
    @classmethod
    def tuple_values_are_unique(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        if len(value) != len(set(value)):
            raise ValueError("execution tuple values must be unique")
        return value

    @field_validator("declared_paths")
    @classmethod
    def declared_paths_are_relative(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or Path(item).is_absolute() for item in value):
            raise ValueError("declared execution paths must be workspace-relative")
        return value

    @field_validator("declared_executables")
    @classmethod
    def declared_executables_are_absolute(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not Path(item).is_absolute() for item in value):
            raise ValueError("declared execution executables must be absolute")
        return value

    @model_validator(mode="after")
    def mode_fields_are_consistent(self) -> Self:
        if self.mode is ExecutionMode.PROCESS:
            if self.executable is None or not Path(self.executable).is_absolute():
                raise ValueError("direct process mode requires an absolute executable")
            if self.script is not None or self.shell_profile is not None:
                raise ValueError("direct process mode does not accept shell fields")
        if self.mode is ExecutionMode.SHELL:
            if self.executable is not None or self.args:
                raise ValueError("shell mode resolves its interpreter and accepts no argv")
            if self.script is None or self.shell_profile is None:
                raise ValueError("shell mode requires a script and versioned shell profile")
            if "\x00" in self.script:
                raise ValueError("shell script must not contain a null byte")
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
