from __future__ import annotations

import stat
from pathlib import Path

import httpx
import pytest

from mishkan.application import ApplicationCommand
from mishkan.config.loader import ConfigLoader
from mishkan.config.models import MishkanConfig, ProjectConfig
from mishkan.config.presets import preset_text
from mishkan.daemon import DaemonBootstrap, create_app
from mishkan.daemon.auth import TokenFile
from mishkan.daemon.bootstrap import DaemonPaths


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _config(tmp_path: Path) -> MishkanConfig:
    source = tmp_path / "config.yaml"
    source.write_text(preset_text("local"), encoding="utf-8")
    loaded = ConfigLoader().load([source]).value
    return loaded.model_copy(update={"project": ProjectConfig(workspace=tmp_path)})


def test_daemon_setup_creates_current_database_and_private_token(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config, principal_id="operator-1")

    assert paths.database.is_file()
    assert stat.S_IMODE(paths.token_file.stat().st_mode) == 0o600
    assert TokenFile(paths.token_file).read().principal_id == "operator-1"


@pytest.mark.anyio
async def test_health_is_public_but_queries_require_authentication(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/v1/health")).status_code == 200
        assert (await client.get("/v1/snapshot")).status_code == 403
        token = TokenFile(paths.token_file).read().token
        response = await client.get("/v1/snapshot", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["cursor"] == 0


@pytest.mark.anyio
async def test_authenticated_command_and_event_query_share_durable_contract(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read().token
    headers = {"Authorization": f"Bearer {token}"}
    command = ApplicationCommand(
        command_type="system.checkpoint",
        actor_id="local-operator",
        target_type="system",
        target_id="local-instance",
        expected_revision=0,
        payload={"checkpoint": "daemon-api"},
    )

    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/v1/commands", headers=headers, json=command.model_dump(mode="json")
        )
        retried = await client.post(
            "/v1/commands", headers=headers, json=command.model_dump(mode="json")
        )
        events = await client.get("/v1/events", headers=headers)

    assert first.status_code == 200
    assert retried.json() == first.json()
    assert events.status_code == 200
    assert len(events.json()["events"]) == 1
    assert events.json()["events"][0]["command_id"] == str(command.command_id)


@pytest.mark.anyio
async def test_command_cannot_claim_another_actor(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read().token
    command = ApplicationCommand(
        command_type="system.checkpoint",
        actor_id="another-actor",
        target_type="system",
        payload={},
    )

    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json=command.model_dump(mode="json"),
        )

    assert response.status_code == 403
    assert response.json()["code"] == "ERR-POL-001"


def test_token_rotation_invalidates_previous_credential(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token_file = TokenFile(paths.token_file)
    previous = token_file.read().token
    current = token_file.rotate().token

    assert previous != current
    assert token_file.authenticate(previous) is None
    assert token_file.authenticate(current) is not None


def test_daemon_paths_are_project_scoped(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonPaths.from_config(config)
    assert paths.database.is_relative_to(tmp_path)
    assert paths.token_file.is_relative_to(tmp_path)
