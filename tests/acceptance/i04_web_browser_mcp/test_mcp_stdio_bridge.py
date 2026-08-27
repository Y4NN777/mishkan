from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import anyio
import pytest
import uvicorn
import yaml
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from mishkan.application import ApplicationCommand
from mishkan.config.loader import ConfigLoader
from mishkan.config.models import MishkanConfig
from mishkan.config.presets import preset_text
from mishkan.daemon import DaemonBootstrap, create_app
from mishkan.daemon.auth import TokenFile


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _config(tmp_path: Path, port: int) -> tuple[MishkanConfig, Path]:
    document = yaml.safe_load(preset_text("local"))
    document["project"]["workspace"] = str(tmp_path)
    document["daemon"]["port"] = port
    source = tmp_path / "config.yaml"
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return ConfigLoader().load([source]).value, source


def _loopback_listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    return listener


@contextmanager
def _running_daemon(config: MishkanConfig, listener: socket.socket) -> Iterator[None]:
    server = uvicorn.Server(uvicorn.Config(create_app(config), log_level="error", lifespan="on"))
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(sockets=[listener])),
        name="mishkand-acceptance",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=10)
        raise RuntimeError("acceptance mishkand did not start")
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("acceptance mishkand did not stop")


@pytest.mark.acceptance
@pytest.mark.anyio
async def test_stdio_bridge_is_a_stateless_client_of_mishkand(tmp_path: Path) -> None:
    listener = _loopback_listener()
    port = int(listener.getsockname()[1])
    config, source = _config(tmp_path, port)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mishkan.mcp.stdio_bridge", "--config", str(source)],
        cwd=tmp_path,
    )
    command = ApplicationCommand(
        command_type="system.checkpoint",
        actor_id=token.principal_id,
        target_type="system",
        target_id="local-instance",
        expected_revision=0,
        payload={"checkpoint": "mcp-stdio-bridge"},
    )

    with _running_daemon(config, listener), anyio.fail_after(30):
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            tools = await session.list_tools()
            health = await session.call_tool("system.health", {})
            result = await session.call_tool(
                "command.submit",
                command.model_dump(mode="json"),
            )

    assert initialized.serverInfo.name == "mishkan"
    assert "command.submit" in {item.name for item in tools.tools}
    assert health.structuredContent == {"status": "ready", "schema": "i04_mcp"}
    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["status"] == "accepted"
