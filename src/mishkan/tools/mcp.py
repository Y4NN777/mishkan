"""Observable MCP session lifecycle and bound-schema drift refusal."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from mishkan.domain.errors import ErrorCode, MishkanError


class McpState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    RECONNECTING = "reconnecting"


class McpSession(Protocol):
    def start(self) -> None: ...

    def list_tools(self) -> tuple[dict[str, Any], ...]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class McpLifecycleEvent:
    event_type: str
    server_id: str
    detail: str


class McpSessionManager:
    def __init__(self, server_id: str, session_factory: Callable[[], McpSession]) -> None:
        self.server_id = server_id
        self._session_factory = session_factory
        self._session: McpSession | None = None
        self._state = McpState.STOPPED
        self._schema_fingerprint: str | None = None
        self._events: list[McpLifecycleEvent] = []

    @property
    def state(self) -> McpState:
        return self._state

    @property
    def events(self) -> tuple[McpLifecycleEvent, ...]:
        return tuple(self._events)

    def connect(self) -> str:
        self._transition(McpState.STARTING, "connection start")
        session = self._session_factory()
        session.start()
        fingerprint = self._fingerprint(session.list_tools())
        self._session = session
        self._schema_fingerprint = fingerprint
        self._transition(McpState.READY, "connection ready")
        return fingerprint

    def reconnect(self, bound_fingerprint: str) -> str:
        self._transition(McpState.RECONNECTING, "connection reconnect")
        if self._session is not None:
            self._session.close()
        session = self._session_factory()
        session.start()
        fingerprint = self._fingerprint(session.list_tools())
        self._session = session
        self._schema_fingerprint = fingerprint
        if fingerprint != bound_fingerprint:
            self._events.append(
                McpLifecycleEvent("mcp.schema_drift", self.server_id, "bound schema changed")
            )
            raise MishkanError(
                ErrorCode.TOOL_DRIFT,
                "MCP schema drifted from the bound registry snapshot",
                details={"server_id": self.server_id},
            )
        self._transition(McpState.READY, "connection ready")
        return fingerprint

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
        self._session = None
        self._transition(McpState.STOPPED, "connection shutdown")

    def _transition(self, state: McpState, detail: str) -> None:
        self._state = state
        self._events.append(McpLifecycleEvent(f"mcp.{state.value}", self.server_id, detail))

    @staticmethod
    def _fingerprint(tools: tuple[dict[str, Any], ...]) -> str:
        return hashlib.sha256(
            json.dumps(tools, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
