"""Versioned request and result contracts shared by execution capability modes."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.config.models import CredentialReference
from mishkan.domain.time import require_aware

_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionMode(StrEnum):
    PROCESS = "process"
    SHELL = "shell"
    PTY = "pty"
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


class ReadinessProbe(ExecutionModel):
    kind: Literal["process_running", "output_contains"] = "process_running"
    value: str | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=3600)

    @model_validator(mode="after")
    def output_probe_has_value(self) -> Self:
        if self.kind == "output_contains" and not self.value:
            raise ValueError("output readiness probe requires a non-empty value")
        if self.kind == "process_running" and self.value is not None:
            raise ValueError("process-running readiness probe accepts no value")
        return self


class ExecutionRequest(ExecutionModel):
    """Common execution envelope; mode-specific public operations constrain its fields."""

    schema_version: str = "1.0"
    execution_id: UUID = Field(default_factory=uuid4)
    mode: ExecutionMode
    cwd: str = Field(default=".", min_length=1)
    executable: str | None = None
    args: tuple[str, ...] = ()
    script: str | None = Field(default=None, min_length=1)
    shell_profile: ShellProfile | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    credential_environment: dict[str, str | CredentialReference] = Field(default_factory=dict)
    credential_references: tuple[CredentialReference, ...] = ()
    stdin: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    expected_exit_codes: tuple[int, ...] = (0,)
    declared_paths: tuple[str, ...] = ()
    declared_executables: tuple[str, ...] = ()
    network_destinations: tuple[str, ...] = ()
    declared_effects: tuple[str, ...] = ()
    output_policy: OutputPolicy | None = None
    owner: str | None = Field(default=None, min_length=1)
    run_id: str | None = Field(default=None, min_length=1)
    task_id: str | None = Field(default=None, min_length=1)
    session_profile: str | None = Field(default=None, min_length=1)
    deadline: datetime | None = None
    rows: int = Field(default=24, ge=1, le=1000)
    columns: int = Field(default=80, ge=1, le=4000)
    readiness: ReadinessProbe | None = None
    policy_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("cwd")
    @classmethod
    def cwd_is_relative(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("execution cwd must be workspace-relative")
        return value

    @field_validator("environment")
    @classmethod
    def environment_names_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not _ENVIRONMENT_NAME.fullmatch(name) for name in value):
            raise ValueError("execution environment names must be portable identifiers")
        return value

    @field_validator("credential_environment")
    @classmethod
    def credential_environment_names_are_valid(
        cls, value: dict[str, str | CredentialReference]
    ) -> dict[str, str | CredentialReference]:
        if any(not _ENVIRONMENT_NAME.fullmatch(name) for name in value):
            raise ValueError("execution credential environment names must be portable identifiers")
        return value

    @field_validator("deadline")
    @classmethod
    def deadline_is_aware(cls, value: datetime | None) -> datetime | None:
        return require_aware(value) if value is not None else None

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
        if any(not item or Path(item).is_absolute() or ".." in Path(item).parts for item in value):
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
            self._require_bounded_operation()
            if any(not isinstance(value, str) for value in self.credential_environment.values()):
                raise ValueError("direct process credential environment uses binding identifiers")
        if self.mode is ExecutionMode.SHELL:
            if self.executable is not None or self.args:
                raise ValueError("shell mode resolves its interpreter and accepts no argv")
            if self.script is None or self.shell_profile is None:
                raise ValueError("shell mode requires a script and versioned shell profile")
            if "\x00" in self.script:
                raise ValueError("shell script must not contain a null byte")
            self._require_bounded_operation()
            if any(not isinstance(value, str) for value in self.credential_environment.values()):
                raise ValueError("shell credential environment uses binding identifiers")
        if self.mode in {ExecutionMode.PTY, ExecutionMode.JOB}:
            if self.executable is None or not Path(self.executable).is_absolute():
                raise ValueError("session mode requires an absolute executable")
            if self.script is not None or self.shell_profile is not None or self.stdin is not None:
                raise ValueError(
                    "session mode accepts input only through explicit session operations"
                )
            required = (
                self.owner,
                self.run_id,
                self.task_id,
                self.session_profile,
                self.deadline,
                self.policy_fingerprint,
            )
            if any(value is None for value in required):
                raise ValueError("session mode requires owner, task, profile, deadline, and policy")
            if any(isinstance(value, str) for value in self.credential_environment.values()):
                raise ValueError(
                    "session credential environment requires typed credential references"
                )
            if self.mode is ExecutionMode.PTY and self.readiness is not None:
                raise ValueError("PTY mode does not accept a managed-job readiness probe")
            if self.mode is ExecutionMode.JOB and (self.rows != 24 or self.columns != 80):
                raise ValueError("managed-job mode does not accept terminal dimensions")
        if set(self.environment) & set(self.credential_environment):
            raise ValueError("plain and credential environment names must not overlap")
        return self

    def _require_bounded_operation(self) -> None:
        if self.timeout_seconds is None or self.output_policy is None:
            raise ValueError("process and shell modes require timeout and output policy")
        session_values = (
            self.owner,
            self.run_id,
            self.task_id,
            self.session_profile,
            self.deadline,
            self.readiness,
            self.policy_fingerprint,
        )
        if any(value is not None for value in session_values) or self.credential_references:
            raise ValueError("process and shell modes do not accept session ownership fields")


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
    stdout_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    stderr_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    stdout_artifact_ref: str | None = Field(default=None, pattern=r"^artifact:[0-9a-f-]{36}$")
    stderr_artifact_ref: str | None = Field(default=None, pattern=r"^artifact:[0-9a-f-]{36}$")
    produced_artifact_refs: tuple[str, ...] = ()
    truncated: bool
    termination_cause: str | None = None
    expected_exit_codes: tuple[int, ...]
    environment_names: tuple[str, ...]
    credential_environment_names: tuple[str, ...]
    declared_effects: tuple[str, ...]
    observed_effects: tuple[str, ...] = ()
    effect_settlement: EffectSettlement
    base_snapshot_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    after_snapshot_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    changed_paths: tuple[str, ...] = ()
    scope_deviations: tuple[str, ...] = ()
    effect_diff_artifact_ref: str | None = Field(default=None, pattern=r"^artifact:[0-9a-f-]{36}$")
    effect_observation_complete: bool = False
    effect_observation_omissions: tuple[str, ...] = ()
    effect_validation: str = "not-applicable"
    retryable: bool = False
    execution_location: str
    error: str | None = None

    @field_validator("started_at", "finished_at")
    @classmethod
    def times_are_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @field_validator(
        "produced_artifact_refs",
        "expected_exit_codes",
        "environment_names",
        "credential_environment_names",
        "declared_effects",
        "observed_effects",
        "changed_paths",
        "scope_deviations",
        "effect_observation_omissions",
    )
    @classmethod
    def result_tuple_values_are_unique(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        if len(value) != len(set(value)):
            raise ValueError("execution result tuple values must be unique")
        return value

    @field_validator("produced_artifact_refs")
    @classmethod
    def produced_artifact_references_are_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for reference in value:
            prefix, separator, raw = reference.partition(":")
            if prefix != "artifact" or not separator:
                raise ValueError("produced artifact reference is invalid")
            UUID(raw)
        return value

    @model_validator(mode="after")
    def settlement_is_temporally_consistent(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("execution result finished before it started")
        if self.exit_code is not None and self.signal is not None:
            raise ValueError("execution result cannot contain both exit code and signal")
        if self.effect_settlement is EffectSettlement.UNCERTAIN and self.retryable:
            raise ValueError("uncertain execution effects cannot be retried blindly")
        return self
