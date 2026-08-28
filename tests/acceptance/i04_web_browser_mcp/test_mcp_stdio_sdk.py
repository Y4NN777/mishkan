from __future__ import annotations

import sys
from pathlib import Path

import pytest
from mcp.types import LATEST_PROTOCOL_VERSION

from mishkan.config.models import McpConnectionConfig, McpProtocolStrategy, McpTransport
from mishkan.mcp import McpPrimitiveKind, McpSdkClient


def _connection(server: Path, *, max_result_bytes: int = 16_384) -> McpConnectionConfig:
    return McpConnectionConfig(
        transport=McpTransport.STDIO,
        protocol_strategy=McpProtocolStrategy.PINNED,
        protocol_versions=(LATEST_PROTOCOL_VERSION,),
        trust="test-fixture",
        exposure_profile="fixture",
        command=sys.executable,
        arguments=(str(server),),
        connect_timeout_seconds=30,
        call_timeout_seconds=30,
        max_result_bytes=max_result_bytes,
    )


@pytest.mark.acceptance
@pytest.mark.anyio
async def test_official_stdio_sdk_discovers_and_invokes_typed_primitives(
    tmp_path: Path,
) -> None:
    server = Path(__file__).parents[2] / "fixtures" / "mcp_test_server.py"
    client = McpSdkClient({})
    configured = _connection(server)

    discovery = await client.discover(
        "fixture",
        configured,
        credentials={},
        workspace=tmp_path,
    )
    observed = {(item.kind, item.name): item for item in discovery.primitives}

    tool = observed[(McpPrimitiveKind.TOOL, "repository.read")]
    assert tool.effect_disposition.value == "read_only"
    assert (McpPrimitiveKind.RESOURCE, "fixture.status") in observed
    assert (McpPrimitiveKind.PROMPT, "review.evidence") in observed

    progress: list[tuple[float, float | None, str | None]] = []

    async def record(value: float, total: float | None, message: str | None) -> None:
        progress.append((value, total, message))

    result = await client.call_tool(
        configured,
        name=tool.name,
        arguments={"path": "src"},
        caller_identity="role:Engineer",
        run_id="run-1",
        task_attempt_id="task-1:attempt-1",
        timeout_seconds=30,
        credentials={},
        workspace=tmp_path,
        progress=record,
    )

    assert result.terminal.value == "immediate"
    assert result.output is not None
    assert result.output["isError"] is False
    assert result.output["structuredContent"] == {"path": "src", "content": "fixture evidence"}
    assert progress == []
