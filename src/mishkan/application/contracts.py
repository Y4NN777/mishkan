"""Versioned command and query envelopes shared by every application client."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mishkan.domain.errors import ErrorEnvelope
from mishkan.domain.identity import new_id
from mishkan.domain.time import require_aware, utc_now


class ApplicationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CommandStatus(StrEnum):
    ACCEPTED = "accepted"
    REFUSED = "refused"
    DUPLICATE = "duplicate"


class ApplicationCommand(ApplicationModel):
    """One idempotent request to mutate authoritative application state."""

    schema_version: str = "1.0"
    command_id: UUID = Field(default_factory=new_id)
    command_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    actor_id: str = Field(min_length=1, max_length=256)
    target_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    target_id: str | None = Field(default=None, min_length=1, max_length=256)
    expected_revision: int | None = Field(default=None, ge=0)
    issued_at: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("issued_at")
    @classmethod
    def issued_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @property
    def fingerprint(self) -> str:
        body = self.model_dump(mode="json", exclude={"command_id"})
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class CommandResult(ApplicationModel):
    schema_version: str = "1.0"
    command_id: UUID
    status: CommandStatus
    target_type: str
    target_id: str | None = None
    revision: int | None = Field(default=None, ge=0)
    event_cursor: int | None = Field(default=None, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    error: ErrorEnvelope | None = None
    completed_at: datetime = Field(default_factory=utc_now)

    @field_validator("completed_at")
    @classmethod
    def completed_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class SnapshotEnvelope(ApplicationModel):
    schema_version: str = "1.0"
    cursor: int = Field(ge=0)
    generated_at: datetime = Field(default_factory=utc_now)
    projections: dict[str, Any]

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)
