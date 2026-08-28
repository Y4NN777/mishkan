from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
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
from mishkan.domain.errors import MishkanError
from mishkan.domain.time import utc_now
from mishkan.mcp import (
    McpCallRequest,
    McpRemoteTaskTerminal,
    McpRepository,
    McpSdkClient,
    McpService,
)
from mishkan.tools.inspection import ContentInspector, InspectionProfileLoader


class _SimulatedDaemonCrash(RuntimeError):
    pass


class _DirectTestCommand:
    def build(
        self,
        workspace: Path,
        command: tuple[str, ...],
        *,
        environment_names: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        del workspace, environment_names
        return command


class _CrashAfterRemoteIdentityClient(McpSdkClient):
    async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
        original = kwargs["remote_task_started"]

        def persist_then_crash(remote_task_id: str) -> None:
            original(remote_task_id)
            raise _SimulatedDaemonCrash

        kwargs["remote_task_started"] = persist_then_crash
        return await super().call_tool(*args, **kwargs)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _config(tmp_path: Path) -> MishkanConfig:
    source = tmp_path / "config.yaml"
    source.write_text(preset_text("local"), encoding="utf-8")
    loaded = ConfigLoader().load([source]).value
    base = loaded.model_copy(update={"project": ProjectConfig(workspace=tmp_path)})
    assert base.mcp is not None
    server = Path(__file__).parents[2] / "fixtures" / "mcp_test_server.py"
    exposure = McpExposureProfileConfig(operations=("repository.read",))
    connection = McpConnectionConfig(
        transport=McpTransport.STDIO,
        protocol_strategy=McpProtocolStrategy.PINNED,
        protocol_versions=(LATEST_PROTOCOL_VERSION,),
        trust="acceptance-fixture",
        exposure_profile="fixture",
        command=sys.executable,
        isolation_profile="acceptance-stdio",
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
    app = create_app(
        config,
        mcp_stdio_commands={"acceptance-stdio": _DirectTestCommand()},
    )
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
        calls = await client.get("/v1/mcp/calls", headers=headers)

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
    assert calls.status_code == 200
    assert calls.json() == []


@pytest.mark.acceptance
@pytest.mark.anyio
async def test_daemon_mcp_connection_command_refuses_payload_credentials(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read()
    app = create_app(
        config,
        mcp_stdio_commands={"acceptance-stdio": _DirectTestCommand()},
    )
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


@pytest.mark.acceptance
@pytest.mark.anyio
async def test_official_sdk_negotiates_and_completes_durable_remote_task(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.mcp is not None
    configured = config.mcp.connections["fixture"]
    state_file = tmp_path / "remote-tasks.json"
    configured = configured.model_copy(
        update={
            "arguments": (*configured.arguments, "--task-state", str(state_file)),
            "remote_tasks_enabled": True,
        }
    )
    client = McpSdkClient(
        {},
        stdio_commands={"acceptance-stdio": _DirectTestCommand()},
    )
    snapshot = await client.discover(
        "fixture",
        configured,
        credentials={},
        workspace=tmp_path,
    )
    task_ids: list[str] = []
    progress_messages: list[str | None] = []

    async def progress(_value: float, _total: float | None, message: str | None) -> None:
        progress_messages.append(message)

    outcome = await client.call_tool(
        configured,
        name="repository.read",
        arguments={"path": "src"},
        caller_identity="role:Engineer",
        run_id="run-1",
        task_attempt_id="task-1:attempt-1",
        timeout_seconds=30,
        credentials={},
        workspace=tmp_path,
        progress=progress,
        remote_task_allowed=True,
        remote_task_id=None,
        remote_task_started=task_ids.append,
        task_poll_min_seconds=0.01,
        task_poll_max_seconds=0.05,
    )
    resumed = await client.call_tool(
        configured,
        name="repository.read",
        arguments={"path": "src"},
        caller_identity="role:Engineer",
        run_id="run-1",
        task_attempt_id="task-1:attempt-1",
        timeout_seconds=30,
        credentials={},
        workspace=tmp_path,
        progress=progress,
        remote_task_allowed=True,
        remote_task_id=outcome.remote_task_id,
        remote_task_started=task_ids.append,
        task_poll_min_seconds=0.01,
        task_poll_max_seconds=0.05,
    )

    assert snapshot.task_tool_calls_supported is True
    assert snapshot.task_cancellation_supported is True
    assert outcome.terminal is McpRemoteTaskTerminal.COMPLETED
    assert outcome.remote_task_id == task_ids[0]
    assert outcome.output is not None
    assert outcome.output["structuredContent"]["content"] == "fixture task evidence"
    assert resumed.remote_task_id == outcome.remote_task_id
    assert resumed.output == outcome.output
    assert len(task_ids) == 1
    assert progress_messages[-1] == "fixture task completed"
    assert state_file.exists()

    cancellation_task_ids: list[str] = []
    pending = asyncio.create_task(
        client.call_tool(
            configured,
            name="repository.read",
            arguments={"path": "wait"},
            caller_identity="role:Engineer",
            run_id="run-2",
            task_attempt_id="task-2:attempt-1",
            timeout_seconds=30,
            credentials={},
            workspace=tmp_path,
            progress=progress,
            remote_task_allowed=True,
            remote_task_id=None,
            remote_task_started=cancellation_task_ids.append,
            task_poll_min_seconds=0.01,
            task_poll_max_seconds=0.05,
        )
    )
    with anyio.fail_after(10):
        while not cancellation_task_ids:
            await anyio.sleep(0.01)
    pending.cancel()
    cancelled = await pending
    confirmed_again = await client.cancel_remote_task(
        configured,
        remote_task_id=cancellation_task_ids[0],
        timeout_seconds=30,
        credentials={},
        workspace=tmp_path,
    )

    assert cancelled.terminal is McpRemoteTaskTerminal.CANCELLED
    assert confirmed_again.terminal is McpRemoteTaskTerminal.CANCELLED
    assert confirmed_again.remote_task_id == cancellation_task_ids[0]


@pytest.mark.acceptance
@pytest.mark.anyio
async def test_daemon_commands_reconcile_and_cancel_remote_tasks_after_transport_loss(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.mcp is not None
    assert config.inspection_profile is not None
    server = Path(__file__).parents[2] / "fixtures" / "mcp_test_server.py"
    state_file = tmp_path / "daemon-remote-tasks.json"
    configured = config.mcp.connections["fixture"].model_copy(
        update={
            "arguments": (str(server), "--task-state", str(state_file)),
            "remote_tasks_enabled": True,
        }
    )
    config = config.model_copy(
        update={"mcp": config.mcp.model_copy(update={"connections": {"fixture": configured}})}
    )
    paths = DaemonBootstrap().setup(config)
    repository = McpRepository(paths.database)
    service = McpService(
        paths.workspace,
        config.mcp,
        repository,
        _CrashAfterRemoteIdentityClient(
            {},
            stdio_commands={"acceptance-stdio": _DirectTestCommand()},
        ),
        ContentInspector(
            InspectionProfileLoader().load(config.inspection_profile, paths.workspace)
        ),
    )
    await service.connect(
        "fixture",
        principal="local-operator",
        policy_fingerprint="policy:acceptance",
        credentials={},
    )
    primitive = next(
        item for item in repository.list_primitives("fixture") if item.name == "repository.read"
    )

    def request(path: str) -> McpCallRequest:
        return McpCallRequest(
            connection_id="fixture",
            primitive_name=primitive.name,
            caller_identity="local-operator",
            run_id=f"run-{path}",
            task_attempt_id=f"task-{path}:attempt-1",
            arguments={"path": path},
            declared_effects=("external_read",),
            effect_disposition=primitive.effect_disposition,
            expected_schema_hash=primitive.schema_hash,
            remote_task_allowed=True,
            deadline=utc_now() + timedelta(seconds=30),
        )

    recoverable = request("src")
    cancellable = request("wait")
    for pending in (recoverable, cancellable):
        with pytest.raises(MishkanError, match="remote MCP task remains recoverable"):
            await service.invoke(pending, credentials={})
    assert service.reconcile_after_restart() == ()

    token = TokenFile(paths.token_file).read()
    headers = {"Authorization": f"Bearer {token.token}"}
    app = create_app(
        config,
        mcp_stdio_commands={"acceptance-stdio": _DirectTestCommand()},
    )
    transport = httpx.ASGITransport(app=app)
    reconcile = ApplicationCommand(
        command_type="mcp.call.reconcile",
        actor_id=token.principal_id,
        target_type="mcp_call",
        target_id=str(recoverable.id),
        expected_revision=0,
        payload={},
    )
    cancel = ApplicationCommand(
        command_type="mcp.call.cancel",
        actor_id=token.principal_id,
        target_type="mcp_call",
        target_id=str(cancellable.id),
        expected_revision=0,
        payload={},
    )

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        reconciled = await client.post(
            "/v1/commands",
            headers=headers,
            json=reconcile.model_dump(mode="json"),
        )
        cancelled = await client.post(
            "/v1/commands",
            headers=headers,
            json=cancel.model_dump(mode="json"),
        )

    assert reconciled.status_code == 200
    assert reconciled.json()["payload"]["state"] == "completed"
    assert cancelled.status_code == 200
    assert cancelled.json()["payload"]["state"] == "cancelled"
    calls = {item["request"]["id"]: item for item in repository.list_calls()}
    assert calls[str(recoverable.id)]["result"]["state"] == "completed"
    assert calls[str(cancellable.id)]["result"]["state"] == "cancelled"
