"""Browser engine port; durable authority remains in BrowserSupervisor."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from mishkan.browser.models import BrowserActionRequest, BrowserTarget
from mishkan.config.models import BrowserProfileConfig


@dataclass(frozen=True, slots=True)
class DriverSession:
    handle: str
    page_ids: tuple[str, ...]
    engine_version: str


@dataclass(frozen=True, slots=True)
class DriverObservation:
    url: str
    title: str
    tree: bytes
    targets: tuple[BrowserTarget, ...]
    screenshot: bytes | None = None


@dataclass(frozen=True, slots=True)
class DriverArtifact:
    channel: str
    media_type: str
    content: bytes


@dataclass(frozen=True, slots=True)
class DriverActionOutcome:
    page_ids: tuple[str, ...]
    artifacts: tuple[DriverArtifact, ...] = ()


@dataclass(frozen=True, slots=True)
class DriverDiagnostics:
    entries: tuple[dict[str, Any], ...]
    next_cursor: int
    truncated: bool


class BrowserDriver(Protocol):
    adapter_id: str

    def open(
        self,
        profile: BrowserProfileConfig,
        *,
        workspace: str,
        initial_url: str | None,
    ) -> DriverSession: ...

    def observe(
        self,
        handle: str,
        page_id: str,
        *,
        screenshot: bool,
    ) -> DriverObservation: ...

    def act(
        self,
        handle: str,
        request: BrowserActionRequest,
        target: BrowserTarget | None,
        *,
        cancellation_requested: Callable[[], bool],
    ) -> DriverActionOutcome: ...

    def diagnostics(
        self,
        handle: str,
        page_id: str,
        channels: tuple[str, ...],
        cursor: int,
        limit: int,
    ) -> DriverDiagnostics: ...

    def close(self, handle: str) -> None: ...


class BrowserUncertainEffect(RuntimeError):
    """The driver lost certainty after a possibly non-idempotent interaction."""


class BrowserOperationCancelled(RuntimeError):
    """The driver proved that cancellation preceded interaction dispatch."""
