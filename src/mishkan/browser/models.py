"""Versioned Browser session, observation, action, and diagnostic contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.config.models import BrowserProfileKind
from mishkan.domain.identity import new_id
from mishkan.domain.time import require_aware, utc_now


class BrowserModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BrowserSessionState(StrEnum):
    OPENING = "opening"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"
    LOST = "lost"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class BrowserActionKind(StrEnum):
    CLICK = "click"
    COORDINATE_CLICK = "coordinate_click"
    FILL = "fill"
    PRESS = "press"
    SELECT = "select"
    CHECK = "check"
    UPLOAD = "upload"
    NAVIGATE = "navigate"
    JAVASCRIPT = "javascript"


class BrowserActionState(StrEnum):
    COMPLETED = "completed"
    REFUSED = "refused"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


class BrowserDiagnosticChannel(StrEnum):
    CONSOLE = "console"
    NETWORK = "network"
    PERFORMANCE = "performance"
    STORAGE = "storage"
    SERVICE_WORKER = "service_worker"


class BrowserSessionRequest(BrowserModel):
    schema_version: str = "1.0"
    profile_id: str = Field(min_length=1)
    owner_identity: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    task_attempt_id: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    initial_url: AnyHttpUrl | None = None
    attached_profile_selected: bool = False


class BrowserSession(BrowserModel):
    schema_version: str = "1.0"
    id: UUID = Field(default_factory=new_id)
    profile_id: str
    profile_kind: BrowserProfileKind
    owner_identity: str
    run_id: str
    task_attempt_id: str
    workspace: str
    adapter: str
    engine: str
    engine_version: str
    state: BrowserSessionState
    revision: int = Field(ge=0)
    page_ids: tuple[str, ...] = ()
    sensitivity: str
    retention: str
    last_error: str | None = None
    uncertain_effect: str | None = None
    profile_state_artifact_reference: str | None = Field(
        default=None,
        pattern=r"^artifact:[0-9a-f-]{36}$",
    )
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def times_are_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class BrowserTarget(BrowserModel):
    reference: str = Field(min_length=1)
    role: str
    name: str
    element_revision: str = Field(min_length=1)
    candidate_effects: tuple[str, ...] = ()
    destination_origin: str | None = Field(
        default=None,
        pattern=r"^https?://[^/?#]+$",
    )


class BrowserObservationRequest(BrowserModel):
    schema_version: str = "1.0"
    session_id: UUID
    page_id: str = Field(min_length=1)
    expected_session_revision: int = Field(ge=0)
    include_screenshot: bool = False


class BrowserObservation(BrowserModel):
    schema_version: str = "1.0"
    id: UUID = Field(default_factory=new_id)
    session_id: UUID
    page_id: str
    session_revision: int = Field(ge=0)
    url: AnyHttpUrl
    title: str
    targets: tuple[BrowserTarget, ...]
    tree_artifact_reference: str
    screenshot_artifact_reference: str | None = None
    engine: str
    engine_version: str
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime

    @field_validator("created_at", "expires_at")
    @classmethod
    def observation_times_are_aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def expiry_follows_creation(self) -> BrowserObservation:
        if self.expires_at <= self.created_at:
            raise ValueError("browser observation expiry must follow creation")
        references = [target.reference for target in self.targets]
        if len(references) != len(set(references)):
            raise ValueError("browser observation target references must be unique")
        return self


class BrowserActionRequest(BrowserModel):
    schema_version: str = "1.0"
    session_id: UUID
    page_id: str = Field(min_length=1)
    observation_id: UUID
    target_reference: str | None = None
    kind: BrowserActionKind
    value: Any = None
    credential_reference: str | None = None
    credential_origin: AnyHttpUrl | None = None
    coordinates: tuple[int, int] | None = None
    visual_evidence_artifact_reference: str | None = Field(
        default=None,
        pattern=r"^artifact:[0-9a-f-]{36}$",
    )
    resolved_effect: str = Field(min_length=1)
    authorized_origins: tuple[str, ...] = ()
    expected_session_revision: int = Field(ge=0)
    idempotency_key: UUID = Field(default_factory=new_id)

    @model_validator(mode="after")
    def target_requirement_matches_action(self) -> BrowserActionRequest:
        targetless = {BrowserActionKind.NAVIGATE, BrowserActionKind.COORDINATE_CLICK}
        if self.kind not in targetless and self.target_reference is None:
            raise ValueError("browser element action requires an observation target")
        if self.kind in targetless and self.target_reference is not None:
            raise ValueError("browser targetless action must not contain an element target")
        coordinate_evidence = (
            self.coordinates is not None or self.visual_evidence_artifact_reference is not None
        )
        if self.kind is BrowserActionKind.COORDINATE_CLICK:
            if self.coordinates is None or self.visual_evidence_artifact_reference is None:
                raise ValueError(
                    "browser coordinate click requires coordinates and visual evidence"
                )
            if any(value < 0 for value in self.coordinates):
                raise ValueError("browser coordinates must be non-negative")
        elif coordinate_evidence:
            raise ValueError("browser visual fallback evidence belongs only to coordinate click")
        if self.credential_reference is not None:
            if self.kind is not BrowserActionKind.FILL:
                raise ValueError("browser credential references are accepted only for fill actions")
            if self.value is not None:
                raise ValueError("browser fill cannot contain a value and credential reference")
        if (self.credential_reference is None) != (self.credential_origin is None):
            raise ValueError("browser credential reference requires its exact authorized origin")
        if len(self.authorized_origins) != len(set(self.authorized_origins)):
            raise ValueError("browser authorized origins must be unique")
        if self.kind is BrowserActionKind.JAVASCRIPT:
            if self.resolved_effect != "script.execute":
                raise ValueError("browser JavaScript requires its separate script.execute effect")
        elif self.resolved_effect == "script.execute":
            raise ValueError("browser script.execute effect belongs only to JavaScript")
        return self


class BrowserActionResult(BrowserModel):
    schema_version: str = "1.0"
    id: UUID = Field(default_factory=new_id)
    request_id: UUID
    session_id: UUID
    page_id: str
    state: BrowserActionState
    resolved_effect: str
    session_revision: int = Field(ge=0)
    observation_invalidated: bool
    artifact_references: tuple[str, ...] = ()
    error_code: str | None = None
    reason: str
    completed_at: datetime = Field(default_factory=utc_now)

    @field_validator("completed_at")
    @classmethod
    def completion_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class BrowserDiagnosticRequest(BrowserModel):
    schema_version: str = "1.0"
    session_id: UUID
    page_id: str = Field(min_length=1)
    channels: tuple[BrowserDiagnosticChannel, ...] = Field(min_length=1)
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1_000_000)

    @field_validator("channels")
    @classmethod
    def channels_are_unique(
        cls,
        value: tuple[BrowserDiagnosticChannel, ...],
    ) -> tuple[BrowserDiagnosticChannel, ...]:
        if len(value) != len(set(value)):
            raise ValueError("browser diagnostic channels must be unique")
        return value


class BrowserDiagnosticResult(BrowserModel):
    schema_version: str = "1.0"
    session_id: UUID
    page_id: str
    cursor: int = Field(ge=0)
    next_cursor: int = Field(ge=0)
    entries: tuple[dict[str, Any], ...]
    truncated: bool
    artifact_reference: str
    engine: str
    engine_version: str
    captured_at: datetime = Field(default_factory=utc_now)

    @field_validator("captured_at")
    @classmethod
    def capture_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)
