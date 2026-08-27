"""Versioned durable event-stream envelopes."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mishkan.domain.time import require_aware


class EventModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EventEnvelope(EventModel):
    schema_version: str = "1.0"
    event_id: UUID
    cursor: int = Field(ge=1)
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    source: str = Field(min_length=1, max_length=128)
    entity_type: str = Field(min_length=1, max_length=64)
    entity_id: str = Field(min_length=1, max_length=256)
    run_id: str | None = Field(default=None, min_length=1, max_length=256)
    task_id: str | None = Field(default=None, min_length=1, max_length=256)
    identity_id: str | None = Field(default=None, min_length=1, max_length=256)
    team_id: str | None = Field(default=None, min_length=1, max_length=256)
    security_relevant: bool = False
    occurred_at: datetime
    command_id: UUID | None = None
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    sensitivity: str = Field(min_length=1, max_length=32)
    payload: dict[str, Any]

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class EventPage(EventModel):
    schema_version: str = "1.0"
    after_cursor: int = Field(ge=0)
    next_cursor: int = Field(ge=0)
    retained_from_cursor: int = Field(ge=0)
    events: tuple[EventEnvelope, ...]
