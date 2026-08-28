"""Versioned durable event-stream envelopes."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class EventHoldScope(StrEnum):
    ALL = "all"
    RUN = "run"
    EVENT = "event"


class EventRetentionPolicy(EventModel):
    """The exact public policy snapshot used to select retention candidates."""

    schema_version: Literal["1.0"] = "1.0"
    max_age_days: int = Field(ge=1, le=36_500)
    batch_size: int = Field(ge=1, le=1_000)
    protect_incomplete_runs: Literal[True] = True

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


class EventHold(EventModel):
    schema_version: Literal["1.0"] = "1.0"
    hold_id: UUID
    scope: EventHoldScope
    scope_id: str | None = Field(default=None, min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=2_048)
    actor_id: str = Field(min_length=1, max_length=256)
    created_at: datetime
    released_at: datetime | None = None

    @field_validator("created_at", "released_at")
    @classmethod
    def times_are_aware(cls, value: datetime | None) -> datetime | None:
        return require_aware(value) if value is not None else None

    @model_validator(mode="after")
    def scope_has_exact_target(self) -> EventHold:
        if self.scope is EventHoldScope.ALL and self.scope_id is not None:
            raise ValueError("an all-events hold cannot declare a scope ID")
        if self.scope is not EventHoldScope.ALL and self.scope_id is None:
            raise ValueError("a targeted event hold requires a scope ID")
        return self


class EventRetentionPlanState(StrEnum):
    PLANNED = "planned"
    APPLIED = "applied"


class EventRetentionPlan(EventModel):
    schema_version: Literal["1.0"] = "1.0"
    plan_id: UUID
    policy: EventRetentionPolicy
    policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    cutoff: datetime
    candidate_event_ids: tuple[UUID, ...]
    candidate_cursors: tuple[int, ...]
    state: EventRetentionPlanState
    deleted_count: int = Field(ge=0)
    created_at: datetime
    applied_at: datetime | None = None

    @field_validator("cutoff", "created_at", "applied_at")
    @classmethod
    def plan_times_are_aware(cls, value: datetime | None) -> datetime | None:
        return require_aware(value) if value is not None else None

    @model_validator(mode="after")
    def plan_is_consistent(self) -> EventRetentionPlan:
        if self.policy_fingerprint != self.policy.fingerprint:
            raise ValueError("retention plan policy fingerprint does not match its policy")
        if len(self.candidate_event_ids) != len(self.candidate_cursors):
            raise ValueError("retention candidate IDs and cursors must have equal length")
        if len(self.candidate_event_ids) > self.policy.batch_size:
            raise ValueError("retention plan exceeds its public batch-size policy")
        if self.state is EventRetentionPlanState.PLANNED and self.applied_at is not None:
            raise ValueError("a planned retention operation cannot have an applied time")
        if self.state is EventRetentionPlanState.APPLIED and self.applied_at is None:
            raise ValueError("an applied retention operation requires an applied time")
        return self
