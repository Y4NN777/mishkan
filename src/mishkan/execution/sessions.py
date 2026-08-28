"""Live projections and precise operations for daemon-owned executions.

The immutable start and settlement payloads are the shared ExecutionRequest and
ExecutionResult contracts. ExecutionSession is only the live lifecycle
projection; it is not a competing request/result envelope.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mishkan.domain.time import require_aware
from mishkan.tools.execution import (
    EffectSettlement,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    ReadinessProbe,
)

# Compatibility imports remain aliases to the single public contracts. New
# callers use ExecutionMode/ExecutionRequest and ExecutionSession directly.
SessionMode = ExecutionMode
SessionRequest = ExecutionRequest
SessionEffectSettlement = EffectSettlement


class SessionState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    READY = "ready"
    CANCELLING = "cancelling"
    SETTLED = "settled"
    FAILED = "failed"
    LOST = "lost"
    UNCERTAIN = "uncertain"


class ExecutionSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    execution_id: UUID
    mode: ExecutionMode
    state: SessionState
    owner: str
    run_id: str
    task_id: str
    cwd: str
    profile: str
    execution_location: Literal["local"] = "local"
    pid: int | None
    process_group_id: int | None
    process_create_time: float | None
    stdout_cursor: int = Field(ge=0)
    stderr_cursor: int = Field(ge=0)
    result: ExecutionResult | None = None
    cancellation_requested: bool = False
    deadline: datetime
    created_at: datetime
    updated_at: datetime

    @field_validator("deadline", "created_at", "updated_at")
    @classmethod
    def record_times_are_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @property
    def session_id(self) -> UUID:
        """Compatibility name for callers created before the unified contract."""

        return self.execution_id

    @property
    def workspace(self) -> str:
        return self.cwd

    @property
    def stdout_artifact_reference(self) -> str | None:
        return self.result.stdout_artifact_ref if self.result is not None else None

    @property
    def stderr_artifact_reference(self) -> str | None:
        return self.result.stderr_artifact_ref if self.result is not None else None

    @property
    def stdout_preview(self) -> str:
        return self.result.stdout_preview if self.result is not None else ""

    @property
    def stderr_preview(self) -> str:
        return self.result.stderr_preview if self.result is not None else ""

    @property
    def declared_effects(self) -> tuple[str, ...]:
        return self.result.declared_effects if self.result is not None else ()

    @property
    def observed_effects(self) -> tuple[str, ...]:
        return self.result.observed_effects if self.result is not None else ()

    @property
    def effect_settlement(self) -> EffectSettlement | None:
        return self.result.effect_settlement if self.result is not None else None

    @property
    def retryable(self) -> bool:
        return self.result.retryable if self.result is not None else False

    @property
    def error(self) -> str | None:
        return self.result.error if self.result is not None else None

    @property
    def exit_code(self) -> int | None:
        return self.result.exit_code if self.result is not None else None

    @property
    def signal(self) -> int | None:
        return self.result.signal if self.result is not None else None


# Python compatibility only; no independent public request/result schema exists.
SessionRecord = ExecutionSession


class CursorRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: UUID
    channel: Literal["stdout", "stderr"]
    offset: int = Field(ge=0)
    next_offset: int = Field(ge=0)
    encoding: Literal["utf-8", "base64"]
    data: str
    eof: bool

    @property
    def session_id(self) -> UUID:
        return self.execution_id


__all__ = [
    "CursorRead",
    "ExecutionSession",
    "ReadinessProbe",
    "SessionEffectSettlement",
    "SessionMode",
    "SessionRecord",
    "SessionRequest",
    "SessionState",
]
