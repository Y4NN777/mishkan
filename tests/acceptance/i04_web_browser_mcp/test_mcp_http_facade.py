from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mishkan.application import ApplicationCommand
from mishkan.config.loader import ConfigLoader
from mishkan.config.models import MishkanConfig, ProjectConfig
from mishkan.config.presets import preset_text
from mishkan.daemon import DaemonBootstrap, create_app
from mishkan.daemon.auth import TokenFile


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _config(tmp_path: Path) -> MishkanConfig:
    source = tmp_path / "config.yaml"
    source.write_text(preset_text("local"), encoding="utf-8")
    loaded = ConfigLoader().load([source]).value
    return loaded.model_copy(update={"project": ProjectConfig(workspace=tmp_path)})


@pytest.mark.acceptance
@pytest.mark.anyio
async def test_official_sdk_uses_authenticated_stateless_daemon_facade(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read()
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {token.token}"}
    authority = f"http://{config.daemon.host}:{config.daemon.port}"
    command = ApplicationCommand(
        command_type="system.checkpoint",
        actor_id=token.principal_id,
        target_type="system",
        target_id="local-instance",
        expected_revision=0,
        payload={"checkpoint": "mcp-http-facade"},
    )

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url=authority,
            headers=headers,
        ) as client,
    ):
        async with (
            streamable_http_client(
                f"{authority}/mcp/",
                http_client=client,
                terminate_on_close=False,
            ) as (read_stream, write_stream, _session_id),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            tools = await session.list_tools()
            resources = await session.list_resources()
            health = await session.call_tool("system.health", {})
            result = await session.call_tool(
                "command.submit",
                command.model_dump(mode="json"),
            )
            snapshot = await session.read_resource("mishkan://snapshot")

        replayed = await client.post(
            "/v1/commands",
            json=command.model_dump(mode="json"),
        )

    assert initialized.serverInfo.name == "mishkan"
    assert [item.name for item in tools.tools] == [
        "system.health",
        "system.snapshot",
        "events.list",
        "run.get",
        "command.submit",
    ]
    assert {str(item.uri) for item in resources.resources} == {
        "mishkan://snapshot",
        "mishkan://runs",
        "mishkan://events",
    }
    assert health.isError is False
    assert health.structuredContent == {"status": "ready", "schema": "i04_mcp"}
    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["status"] == "accepted"
    assert snapshot.contents[0].mimeType == "application/json"
    assert replayed.status_code == 200
    assert replayed.json() == result.structuredContent


@pytest.mark.acceptance
@pytest.mark.anyio
async def test_mcp_mount_rejects_missing_daemon_bearer(tmp_path: Path) -> None:
    config = _config(tmp_path)
    DaemonBootstrap().setup(config)
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    authority = f"http://{config.daemon.host}:{config.daemon.port}"

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url=authority) as client,
    ):
        response = await client.post(
            "/mcp/",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "unauthenticated", "version": "1"},
                },
            },
        )

    assert response.status_code == 403
    assert response.json()["code"] == "ERR-POL-001"
