from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mishkan.application import ApplicationCommand
from mishkan.config.loader import ConfigLoader
from mishkan.config.models import MishkanConfig, ProjectConfig
from mishkan.config.presets import preset_text
from mishkan.daemon import DaemonBootstrap, create_app
from mishkan.daemon.auth import TokenFile
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.mcp import DaemonMcpFacade


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _config(tmp_path: Path) -> MishkanConfig:
    source = tmp_path / "config.yaml"
    source.write_text(preset_text("local"), encoding="utf-8")
    loaded = ConfigLoader().load([source]).value
    return loaded.model_copy(update={"project": ProjectConfig(workspace=tmp_path)})


@pytest.mark.anyio
async def test_remote_facade_forwards_queries_commands_and_resources(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token_file = TokenFile(paths.token_file)
    token = token_file.read()
    assert config.mcp is not None
    assert config.daemon is not None
    facade = DaemonMcpFacade(
        config.mcp,
        config.daemon,
        token_file,
        transport=httpx.ASGITransport(app=create_app(config)),
    )
    command = ApplicationCommand(
        command_type="system.checkpoint",
        actor_id=token.principal_id,
        target_type="system",
        target_id="local-instance",
        expected_revision=0,
        payload={"checkpoint": "remote-facade"},
    )

    health = await facade.invoke("system.health", {}, principal_id=token.principal_id)
    result = await facade.invoke(
        "command.submit",
        command.model_dump(mode="json"),
        principal_id=token.principal_id,
    )
    events = await facade.read_resource("mishkan://events", principal_id=token.principal_id)

    assert health == {"status": "ready", "schema": "i04_mcp"}
    assert result["status"] == "accepted"
    assert len(events["events"]) == 1


@pytest.mark.anyio
async def test_remote_facade_rejects_hidden_operations_and_identity_mismatch(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token_file = TokenFile(paths.token_file)
    token = token_file.read()
    assert config.mcp is not None
    assert config.daemon is not None
    facade = DaemonMcpFacade(
        config.mcp,
        config.daemon,
        token_file,
        transport=httpx.ASGITransport(app=create_app(config)),
    )

    with pytest.raises(MishkanError) as hidden:
        await facade.invoke("events.stream", {}, principal_id=token.principal_id)
    with pytest.raises(MishkanError) as identity:
        await facade.invoke("system.health", {}, principal_id="another-client")

    assert hidden.value.envelope.code is ErrorCode.AUTHORITY_NOT_GRANTED
    assert identity.value.envelope.code is ErrorCode.AUTHORITY_NOT_GRANTED
