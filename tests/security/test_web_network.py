from __future__ import annotations

import http.server
import threading
from collections.abc import Iterable

import httpcore
import pytest

from mishkan.config.models import NetworkProfileConfig
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.web.network import (
    GuardedNetworkBackend,
    HttpxWebTransport,
    NetworkGuard,
)


class StaticResolver:
    def __init__(self, *addresses: str) -> None:
        self.addresses = addresses

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        del host, port
        return self.addresses


def _profile(
    *,
    ports: tuple[int, ...] = (80, 443),
    public: bool = False,
    private: bool = False,
    loopback: bool = False,
) -> NetworkProfileConfig:
    return NetworkProfileConfig(
        allowed_schemes=("http", "https"),
        allowed_ports=ports,
        allow_public=public,
        allow_private=private,
        allow_loopback=loopback,
        allow_link_local=False,
        allow_multicast=False,
        max_redirects=3,
        connect_timeout_seconds=2,
        read_timeout_seconds=2,
        max_response_bytes=1024,
        max_decompressed_bytes=2048,
        max_concurrency=1,
        credential_header_names=("authorization", "cookie"),
    )


def test_public_profile_refuses_any_mixed_private_dns_answer() -> None:
    guard = NetworkGuard(
        _profile(public=True),
        StaticResolver("93.184.216.34", "127.0.0.1"),
    )

    with pytest.raises(MishkanError) as caught:
        guard.resolve_allowed("example.test", 443)

    assert caught.value.envelope.code is ErrorCode.WEB
    assert caught.value.envelope.details["category"] == "loopback"


def test_private_permission_does_not_implicitly_allow_link_local() -> None:
    guard = NetworkGuard(_profile(private=True), StaticResolver("169.254.10.20"))

    with pytest.raises(MishkanError) as caught:
        guard.resolve_allowed("metadata.test", 80)

    assert caught.value.envelope.code is ErrorCode.WEB
    assert caught.value.envelope.details["category"] == "link_local"


class PeerStream(httpcore.NetworkStream):
    def __init__(self, peer: str) -> None:
        self.peer = peer
        self.closed = False

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        del max_bytes, timeout
        return b""

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        del buffer, timeout

    def close(self) -> None:
        self.closed = True

    def start_tls(
        self,
        ssl_context: object,
        server_hostname: bytes | str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        del ssl_context, server_hostname, timeout
        return self

    def get_extra_info(self, info: str) -> object:
        return (self.peer, 443) if info == "server_addr" else None


class PeerBackend(httpcore.NetworkBackend):
    def __init__(self, peer: str) -> None:
        self.stream = PeerStream(peer)

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[
            tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
        ]
        | None = None,
    ) -> httpcore.NetworkStream:
        del host, port, timeout, local_address, socket_options
        return self.stream

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[
            tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
        ]
        | None = None,
    ) -> httpcore.NetworkStream:
        del path, timeout, socket_options
        raise AssertionError("unexpected Unix connection")

    def sleep(self, seconds: float) -> None:
        del seconds


def test_connected_peer_must_equal_the_dns_locked_address() -> None:
    backend = PeerBackend("127.0.0.2")
    guard = NetworkGuard(_profile(loopback=True), StaticResolver("127.0.0.1"))
    locked = GuardedNetworkBackend(guard, backend)

    with pytest.raises(httpcore.ConnectError):
        locked.connect_tcp("service.test", 443)

    assert backend.stream.closed
    assert locked.failure is not None
    assert locked.failure.envelope.code is ErrorCode.WEB


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        payload = b"bounded"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def test_httpx_transport_records_the_real_loopback_peer() -> None:
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("execution sandbox does not permit loopback sockets")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        result = HttpxWebTransport().request(
            "GET",
            f"http://127.0.0.1:{port}/evidence",
            profile=_profile(ports=(port,), loopback=True),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.content == b"bounded"
    assert result.connection.connected_address == "127.0.0.1"
    assert result.connection.dns_answers == ("127.0.0.1",)
