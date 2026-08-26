"""mishkand process entry point."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from mishkan.config.loader import ConfigLoader
from mishkan.daemon.api import create_app
from mishkan.daemon.bootstrap import DaemonPaths


def main() -> None:
    parser = argparse.ArgumentParser(prog="mishkand")
    parser.add_argument("--config", "-c", action="append", type=Path)
    arguments = parser.parse_args()
    encoded = os.environ.get("MISHKAN_CONFIG", "")
    sources = tuple(arguments.config or (Path(item) for item in encoded.split(os.pathsep) if item))
    effective = ConfigLoader().load(sources)
    paths = DaemonPaths.from_config(effective.value)
    del paths
    daemon = effective.value.daemon
    assert daemon is not None
    uvicorn.run(create_app(effective.value), host=daemon.host, port=daemon.port)


if __name__ == "__main__":
    main()
