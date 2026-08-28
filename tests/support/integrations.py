from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn

from mishkan.config.models import MishkanConfig
from mishkan.daemon import create_app


def loopback_listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    return listener


@contextmanager
def running_daemon(config: MishkanConfig, listener: socket.socket) -> Iterator[None]:
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
