"""Public PTY and managed-job lifecycle contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from mishkan.config.models import CredentialReference


class SessionMode(StrEnum):
    PTY = "pty"
    JOB = "job"


class SessionState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    READY = "ready"
    CANCELLING = "cancelling"
    SETTLED = "settled"
    FAILED = "failed"
    LOST = "lost"
    UNCERTAIN = "uncertain"


class ReadinessProbe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["process_running", "output_contains"] = "process_running"
    value: str | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=3600)


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: SessionMode
    owner: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    workspace: str = "."
    executable: str = Field(min_length=1)
    arguments: tuple[str, ...] = ()
    environment: dict[str, str] = Field(default_factory=dict)
    credential_environment: dict[str, CredentialReference] = Field(default_factory=dict)
    credential_references: tuple[CredentialReference, ...] = ()
    profile: str = Field(min_length=1)
    deadline: datetime
    rows: int = Field(default=24, ge=1, le=1000)
    columns: int = Field(default=80, ge=1, le=4000)
    readiness: ReadinessProbe | None = None
    policy_fingerprint: str = Field(min_length=64, max_length=64)


class SessionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    session_id: UUID
    mode: SessionMode
    state: SessionState
    owner: str
    run_id: str
    task_id: str
    pid: int | None
    process_group_id: int | None
    process_create_time: float | None
    stdout_cursor: int = Field(ge=0)
    stderr_cursor: int = Field(ge=0)
    exit_code: int | None = None
    signal: int | None = None
    stdout_artifact_reference: str | None = None
    stderr_artifact_reference: str | None = None
    cancellation_requested: bool = False
    created_at: datetime
    updated_at: datetime


class CursorRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: UUID
    channel: Literal["stdout", "stderr"]
    offset: int = Field(ge=0)
    next_offset: int = Field(ge=0)
    encoding: Literal["utf-8", "base64"]
    data: str
    eof: bool
