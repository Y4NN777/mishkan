"""Stateless STDIO MCP bridge that delegates every operation to mishkand."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import anyio
from mcp.server.stdio import stdio_server

from mishkan.config.loader import ConfigLoader
from mishkan.config.models import MishkanConfig
from mishkan.daemon.auth import TokenFile
from mishkan.daemon.bootstrap import DaemonPaths
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.mcp.remote import DaemonMcpFacade
from mishkan.mcp.server import McpProtocolFacade


async def serve(config: MishkanConfig) -> None:
    daemon = config.daemon
    mcp = config.mcp
    if daemon is None or mcp is None:
        raise MishkanError(ErrorCode.CONFIGURATION, "I04 daemon and MCP configuration is required")
    if not mcp.facade.enabled or not mcp.facade.stdio_bridge_enabled:
        raise MishkanError(ErrorCode.AUTHORITY_NOT_GRANTED, "MCP STDIO bridge is disabled")
    paths = DaemonPaths.from_config(config)
    token_file = TokenFile(paths.token_file)
    token_file.read()
    facade = DaemonMcpFacade(mcp, daemon, token_file)
    protocol = McpProtocolFacade(facade, lambda: token_file.read().principal_id)
    async with stdio_server() as (read_stream, write_stream):
        await protocol.server.run(
            read_stream,
            write_stream,
            protocol.server.create_initialization_options(),
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="mishkan-mcp-stdio")
    parser.add_argument("--config", "-c", action="append", type=Path)
    arguments = parser.parse_args()
    encoded = os.environ.get("MISHKAN_CONFIG", "")
    sources = tuple(arguments.config or (Path(item) for item in encoded.split(os.pathsep) if item))
    try:
        effective = ConfigLoader().load(sources)
        anyio.run(serve, effective.value)
    except MishkanError as error:
        print(error.envelope.model_dump_json(), file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
