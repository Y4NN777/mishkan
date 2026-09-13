"""Derived notification projections over the immutable event stream."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NotificationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NotificationSeverity(StrEnum):
    INFORMATION = "information"
    ATTENTION = "attention"
    ACTION_REQUIRED = "action_required"
    URGENT = "urgent"


class NotificationDelivery(StrEnum):
    FEED = "feed"
    SILENT = "silent"


class NotificationRuleConfig(NotificationModel):
    rule_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    event_types: tuple[str, ...] = Field(min_length=1)
    sources: tuple[str, ...] = ()
    severity: NotificationSeverity
    delivery: NotificationDelivery

    @model_validator(mode="after")
    def validate_patterns(self) -> NotificationRuleConfig:
        if any(not pattern.strip() for pattern in self.event_types):
            raise ValueError("notification event patterns must not be blank")
        if any(not source.strip() for source in self.sources):
            raise ValueError("notification source patterns must not be blank")
        if len(set(self.event_types)) != len(self.event_types):
            raise ValueError("notification event patterns must be unique within a rule")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("notification source patterns must be unique within a rule")
        return self


class NotificationConfig(NotificationModel):
    default_severity: NotificationSeverity = NotificationSeverity.INFORMATION
    default_delivery: NotificationDelivery = NotificationDelivery.FEED
    rules: tuple[NotificationRuleConfig, ...] = ()
    page_limit: int = Field(default=100, ge=1, le=1_000)

    @model_validator(mode="after")
    def validate_rule_ids(self) -> NotificationConfig:
        rule_ids = [rule.rule_id for rule in self.rules]
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError("notification rule identifiers must be unique")
        return self


class NotificationRecord(NotificationModel):
    schema_version: Literal["1.0"] = "1.0"
    notification_id: UUID
    event_id: UUID
    event_cursor: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=128)
    entity_type: str = Field(min_length=1, max_length=64)
    entity_id: str = Field(min_length=1, max_length=256)
    severity: NotificationSeverity
    delivery: NotificationDelivery
    rule_id: str | None = Field(default=None, min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=4_096)
    action_reference: str | None = Field(default=None, min_length=1, max_length=1_024)


class NotificationPage(NotificationModel):
    schema_version: Literal["1.0"] = "1.0"
    after_cursor: int = Field(ge=0)
    next_cursor: int = Field(ge=0)
    retained_from_cursor: int = Field(ge=0)
    notifications: tuple[NotificationRecord, ...]
