from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from mcp.types import LATEST_PROTOCOL_VERSION

from mishkan.application import ApplicationCommand
from mishkan.config.loader import ConfigLoader
from mishkan.config.models import (
    McpConnectionConfig,
    McpExposureProfileConfig,
    McpProtocolStrategy,
    McpTransport,
    MishkanConfig,
    ProjectConfig,
)
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
    base = loaded.model_copy(update={"project": ProjectConfig(workspace=tmp_path)})
    assert base.mcp is not None
    server = Path(__file__).parents[2] / "fixtures" / "i04_mcp_server.py"
    exposure = McpExposureProfileConfig(operations=("repository.read",))
    connection = McpConnectionConfig(
        transport=McpTransport.STDIO,
        protocol_strategy=McpProtocolStrategy.PINNED,
        protocol_versions=(LATEST_PROTOCOL_VERSION,),
        trust="acceptance-fixture",
        exposure_profile="fixture",
        command=sys.executable,
        arguments=(str(server),),
        connect_timeout_seconds=30,
        call_timeout_seconds=30,
        max_result_bytes=16_384,
    )
    mcp = base.mcp.model_copy(
        update={
            "connections": {"fixture": connection},
            "exposure_profiles": {
                **base.mcp.exposure_profiles,
                "fixture": exposure,
            },
        }
    )
    return base.model_copy(update={"mcp": mcp})


@pytest.mark.acceptance
@pytest.mark.anyio
async def test_daemon_connect_command_discovers_without_receiving_secret_values(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read()
    headers = {"Authorization": f"Bearer {token.token}"}
    command = ApplicationCommand(
        command_type="mcp.connection.connect",
        actor_id=token.principal_id,
        target_type="mcp_connection",
        target_id="fixture",
        expected_revision=0,
        payload={},
    )
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        connected = await client.post(
            "/v1/commands",
            headers=headers,
            json=command.model_dump(mode="json"),
        )
        connections = await client.get("/v1/mcp/connections", headers=headers)
        primitives = await client.get(
            "/v1/mcp/connections/fixture/primitives",
            headers=headers,
        )
        contracts = await client.get(
            "/v1/mcp/connections/fixture/contracts",
            headers=headers,
        )

    assert connected.status_code == 200
    assert connected.json()["payload"]["state"] == "ready"
    assert connections.json()[0]["credential_principal"] == token.principal_id
    assert {item["name"] for item in primitives.json()} == {
        "fixture.status",
        "repository.read",
        "review.evidence",
    }
    assert len(contracts.json()) == 1
    assert contracts.json()[0]["adapter"] == "mcp.outbound.tool"
    assert contracts.json()[0]["adapter_config"]["primitive_name"] == "repository.read"


@pytest.mark.acceptance
@pytest.mark.anyio
async def test_daemon_mcp_connection_command_refuses_payload_credentials(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read()
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    command = ApplicationCommand(
        command_type="mcp.connection.connect",
        actor_id=token.principal_id,
        target_type="mcp_connection",
        target_id="fixture",
        expected_revision=0,
        payload={"token": "must-not-cross-command-boundary"},
    )

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        response = await client.post(
            "/v1/commands",
            headers={"Authorization": f"Bearer {token.token}"},
            json=command.model_dump(mode="json"),
        )

    assert response.status_code == 422
    assert response.json()["code"] == "ERR-OUT-001"
