from __future__ import annotations

from collections.abc import Iterator

import pytest

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.tools.mcp import McpSessionManager, McpState


class FakeSession:
    def __init__(self, schema: tuple[dict[str, object], ...]) -> None:
        self.schema = schema
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def list_tools(self) -> tuple[dict[str, object], ...]:
        return self.schema

    def close(self) -> None:
        self.closed = True


def test_mcp_lifecycle_is_observable_and_same_schema_reconnects() -> None:
    schema: tuple[dict[str, object], ...] = ({"name": "read", "inputSchema": {"type": "object"}},)
    manager = McpSessionManager("graph", lambda: FakeSession(schema))

    fingerprint = manager.connect()
    assert manager.reconnect(fingerprint) == fingerprint
    manager.close()

    assert manager.state is McpState.STOPPED
    assert tuple(event.event_type for event in manager.events) == (
        "mcp.starting",
        "mcp.ready",
        "mcp.reconnecting",
        "mcp.ready",
        "mcp.stopped",
    )


def test_mcp_schema_drift_blocks_bound_session() -> None:
    first: tuple[dict[str, object], ...] = ({"name": "read", "inputSchema": {"type": "object"}},)
    second: tuple[dict[str, object], ...] = (
        {"name": "read", "inputSchema": {"type": "object", "required": ["path"]}},
    )
    schemas: Iterator[tuple[dict[str, object], ...]] = iter((first, second))
    manager = McpSessionManager("graph", lambda: FakeSession(next(schemas)))
    fingerprint = manager.connect()

    with pytest.raises(MishkanError) as caught:
        manager.reconnect(fingerprint)

    assert caught.value.envelope.code is ErrorCode.TOOL_DRIFT
    assert manager.events[-1].event_type == "mcp.schema_drift"
