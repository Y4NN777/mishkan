from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import AnyHttpUrl
from support.integrations import loopback_listener, running_daemon

from mishkan.application import ApplicationCommand
from mishkan.config.loader import ConfigLoader
from mishkan.config.models import (
    CredentialReference,
    CredentialSource,
    McpConnectionConfig,
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


def _preset(tmp_path: Path) -> MishkanConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "config.yaml"
    source.write_text(preset_text("local"), encoding="utf-8")
    loaded = ConfigLoader().load([source]).value
    return loaded.model_copy(update={"project": ProjectConfig(workspace=tmp_path)})


@pytest.mark.acceptance
@pytest.mark.anyio
async def test_daemon_discovers_streamable_http_peer_through_dns_locked_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_listener = loopback_listener()
    remote_port = int(remote_listener.getsockname()[1])
    remote = _preset(tmp_path / "remote")
    assert remote.daemon is not None
    remote = remote.model_copy(
        update={"daemon": remote.daemon.model_copy(update={"port": remote_port})}
    )
    remote_paths = DaemonBootstrap().setup(remote, principal_id="remote-harness")
    remote_token = TokenFile(remote_paths.token_file).read().token

    local = _preset(tmp_path / "local")
    assert local.mcp is not None
    assert local.web is not None
    local_profile = local.web.network_profiles["local-services"]
    web = local.web.model_copy(
        update={
            "network_profiles": {
                **local.web.network_profiles,
                "local-services": local_profile.model_copy(
                    update={"allowed_ports": (*local_profile.allowed_ports, remote_port)}
                ),
            }
        }
    )
    credential = CredentialReference(
        source=CredentialSource.ENV,
        locator="MISHKAN_ACCEPTANCE_REMOTE_BEARER",
    )
    connection = McpConnectionConfig(
        transport=McpTransport.STREAMABLE_HTTP,
        protocol_strategy=McpProtocolStrategy.PINNED,
        protocol_versions=(LATEST_PROTOCOL_VERSION,),
        trust="acceptance-loopback",
        exposure_profile="governed-harness",
        credential_refs=(credential,),
        network_profile="local-services",
        endpoint=AnyHttpUrl(f"http://127.0.0.1:{remote_port}/mcp/"),
        headers={"Authorization": credential},
        connect_timeout_seconds=30,
        call_timeout_seconds=30,
        max_result_bytes=65_536,
    )
    mcp = local.mcp.model_copy(update={"connections": {"remote-daemon": connection}})
    local = local.model_copy(update={"web": web, "mcp": mcp})
    local_paths = DaemonBootstrap().setup(local, principal_id="local-operator")
    local_token = TokenFile(local_paths.token_file).read()
    monkeypatch.setenv("MISHKAN_ACCEPTANCE_REMOTE_BEARER", f"Bearer {remote_token}")
    app = create_app(local)
    transport = httpx.ASGITransport(app=app)
    command = ApplicationCommand(
        command_type="mcp.connection.connect",
        actor_id=local_token.principal_id,
        target_type="mcp_connection",
        target_id="remote-daemon",
        expected_revision=0,
        payload={},
    )

    with running_daemon(remote, remote_listener):
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://local") as client,
        ):
            connected = await client.post(
                "/v1/commands",
                headers={"Authorization": f"Bearer {local_token.token}"},
                json=command.model_dump(mode="json"),
            )
            primitives = await client.get(
                "/v1/mcp/connections/remote-daemon/primitives",
                headers={"Authorization": f"Bearer {local_token.token}"},
            )

    assert connected.status_code == 200
    assert connected.json()["payload"]["transport"] == "streamable_http"
    assert connected.json()["payload"]["state"] == "ready"
    assert {item["name"] for item in primitives.json()} >= {
        "system.health",
        "command.submit",
    }
