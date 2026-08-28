"""DNS-locked HTTPX transport and public network-profile enforcement."""

from __future__ import annotations

import ipaddress
import re
import socket
import ssl
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpcore
import httpx

from mishkan.config.models import NetworkProfileConfig
from mishkan.domain.errors import ErrorCode, MishkanError

_HTTP_FIELD_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_TRANSPORT_CONTROLLED_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def validate_outbound_headers(headers: Mapping[str, str] | None) -> None:
    """Keep HTTP routing and message framing under the verified transport's control."""

    for name, value in (headers or {}).items():
        normalized = name.casefold()
        if not _HTTP_FIELD_NAME.fullmatch(name) or "\r" in value or "\n" in value:
            raise MishkanError(
                ErrorCode.WEB,
                "web request contains an invalid HTTP header",
                details={"header": name},
            )
        if normalized in _TRANSPORT_CONTROLLED_HEADERS:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "HTTP routing and framing headers are controlled by the verified transport",
                details={"header": normalized},
            )


class Resolver(Protocol):
    def resolve(self, host: str, port: int) -> tuple[str, ...]: ...


class SystemResolver:
    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        try:
            answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise MishkanError(
                ErrorCode.WEB,
                "web destination DNS resolution failed",
                details={"host": host, "category": "dns"},
                retryable=True,
            ) from exc
        addresses = tuple(dict.fromkeys(cast(str, item[4][0]) for item in answers))
        if not addresses:
            raise MishkanError(
                ErrorCode.WEB,
                "web destination DNS resolution returned no address",
                details={"host": host, "category": "dns"},
                retryable=True,
            )
        return addresses


@dataclass(frozen=True, slots=True)
class ValidatedUrl:
    value: str
    origin: str
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class ConnectionEvidence:
    dns_answers: tuple[str, ...]
    connected_address: str


@dataclass(frozen=True, slots=True)
class HttpExchange:
    status_code: int
    headers: dict[str, str]
    content: bytes
    wire_bytes: int
    decoded_bytes: int
    connection: ConnectionEvidence


class NetworkGuard:
    def __init__(self, profile: NetworkProfileConfig, resolver: Resolver | None = None) -> None:
        self.profile = profile
        self.resolver = resolver or SystemResolver()

    def validate_url(self, raw_url: str) -> ValidatedUrl:
        try:
            url = httpx.URL(raw_url)
        except (TypeError, ValueError) as exc:
            raise MishkanError(ErrorCode.WEB, "web URL is invalid") from exc
        scheme = url.scheme.casefold()
        if scheme not in self.profile.allowed_schemes:
            raise MishkanError(
                ErrorCode.WEB,
                "web URL scheme is outside the configured network profile",
                details={"scheme": scheme},
            )
        if url.userinfo or not url.host:
            raise MishkanError(
                ErrorCode.WEB,
                "web URL must contain a host and no embedded credentials",
            )
        host = url.host.encode("idna").decode("ascii").casefold().rstrip(".")
        port = url.port or (443 if scheme == "https" else 80)
        if port not in self.profile.allowed_ports:
            raise MishkanError(
                ErrorCode.WEB,
                "web destination port is outside the configured network profile",
                details={"port": port},
            )
        normalized = url.copy_with(host=host, port=port)
        default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
        origin = f"{scheme}://{host}" if default_port else f"{scheme}://{host}:{port}"
        return ValidatedUrl(str(normalized), origin, host, port)

    def resolve_allowed(self, host: str, port: int) -> tuple[str, ...]:
        answers = self.resolver.resolve(host, port)
        for answer in answers:
            self.validate_address(answer)
        return answers

    def validate_address(self, raw_address: str) -> None:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise MishkanError(
                ErrorCode.WEB,
                "DNS returned a non-IP destination",
                details={"category": "dns"},
            ) from exc
        category = self._category(address)
        allowed_by_category = {
            "public": self.profile.allow_public,
            "private": self.profile.allow_private,
            "loopback": self.profile.allow_loopback,
            "link_local": self.profile.allow_link_local,
            "multicast": self.profile.allow_multicast,
            "non_routable": False,
        }
        if not allowed_by_category[category]:
            raise MishkanError(
                ErrorCode.WEB,
                "web destination address is outside the configured network profile",
                details={"address": str(address), "category": category},
            )

    @staticmethod
    def _category(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
        if address.is_loopback:
            return "loopback"
        if address.is_link_local:
            return "link_local"
        if address.is_multicast:
            return "multicast"
        if address.is_private:
            return "private"
        if address.is_global:
            return "public"
        return "non_routable"


class GuardedNetworkBackend(httpcore.NetworkBackend):
    """Resolve once, connect to that address, then prove the actual peer before HTTP bytes."""

    def __init__(
        self,
        guard: NetworkGuard,
        backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._guard = guard
        self._backend = backend or httpcore.SyncBackend()
        self.evidence: ConnectionEvidence | None = None
        self.failure: MishkanError | None = None

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
        try:
            answers = self._guard.resolve_allowed(host, port)
        except MishkanError as error:
            self.failure = error
            raise httpcore.ConnectError("network profile refused DNS answers") from error
        last_error: Exception | None = None
        for address in answers:
            try:
                stream = self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
                server_address: Any = stream.get_extra_info("server_addr")
                peer = str(server_address[0]) if isinstance(server_address, tuple) else ""
                self._guard.validate_address(peer)
                if ipaddress.ip_address(peer) != ipaddress.ip_address(address):
                    stream.close()
                    raise MishkanError(
                        ErrorCode.WEB,
                        "connected web peer differs from the DNS-locked destination",
                        details={"expected": address, "connected": peer},
                    )
                self.evidence = ConnectionEvidence(answers, peer)
                return stream
            except MishkanError as error:
                self.failure = error
                raise httpcore.ConnectError("network profile refused connected peer") from error
            except (OSError, httpcore.NetworkError) as exc:
                last_error = exc
        raise httpcore.ConnectError("all DNS-locked destination addresses failed") from last_error

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
        raise httpcore.UnsupportedProtocol("web transport does not accept Unix sockets")

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class GuardedAsyncNetworkBackend(httpcore.AsyncNetworkBackend):
    """Async peer-verified equivalent used by long-lived HTTP protocols such as MCP."""

    def __init__(
        self,
        guard: NetworkGuard,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._guard = guard
        self._backend = backend or httpcore.AnyIOBackend()
        self.evidence: ConnectionEvidence | None = None
        self.failure: MishkanError | None = None

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[
            tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
        ]
        | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            answers = self._guard.resolve_allowed(host, port)
        except MishkanError as error:
            self.failure = error
            raise httpcore.ConnectError("network profile refused DNS answers") from error
        last_error: Exception | None = None
        for address in answers:
            try:
                stream = await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
                server_address: Any = stream.get_extra_info("server_addr")
                peer = str(server_address[0]) if isinstance(server_address, tuple) else ""
                self._guard.validate_address(peer)
                if ipaddress.ip_address(peer) != ipaddress.ip_address(address):
                    await stream.aclose()
                    raise MishkanError(
                        ErrorCode.WEB,
                        "connected web peer differs from the DNS-locked destination",
                        details={"expected": address, "connected": peer},
                    )
                self.evidence = ConnectionEvidence(answers, peer)
                return stream
            except MishkanError as error:
                self.failure = error
                raise httpcore.ConnectError("network profile refused connected peer") from error
            except (OSError, httpcore.NetworkError) as exc:
                last_error = exc
        raise httpcore.ConnectError("all DNS-locked destination addresses failed") from last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[
            tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
        ]
        | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del path, timeout, socket_options
        raise httpcore.UnsupportedProtocol("web transport does not accept Unix sockets")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class GuardedHTTPTransport(httpx.HTTPTransport):
    def __init__(self, guard: NetworkGuard) -> None:
        super().__init__(retries=0)
        self._pool.close()
        self.backend = GuardedNetworkBackend(guard)
        self._pool = httpcore.ConnectionPool(
            max_connections=1,
            max_keepalive_connections=0,
            retries=0,
            network_backend=self.backend,
        )


class GuardedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, guard: NetworkGuard) -> None:
        super().__init__(trust_env=False, retries=0)
        self.backend = GuardedAsyncNetworkBackend(guard)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=1,
            max_keepalive_connections=1,
            retries=0,
            network_backend=self.backend,
        )


class HttpxWebTransport:
    """Perform one bounded, non-redirecting HTTP exchange through a DNS-locked transport."""

    def __init__(self, resolver: Resolver | None = None) -> None:
        self._resolver = resolver

    def request(
        self,
        method: str,
        url: str,
        *,
        profile: NetworkProfileConfig,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        timeout_seconds: float | None = None,
    ) -> HttpExchange:
        guard = NetworkGuard(profile, self._resolver)
        target = guard.validate_url(url)
        validate_outbound_headers(headers)
        transport = GuardedHTTPTransport(guard)
        timeout = httpx.Timeout(
            timeout_seconds or profile.read_timeout_seconds,
            connect=profile.connect_timeout_seconds,
        )
        try:
            with (
                httpx.Client(
                    transport=transport,
                    follow_redirects=False,
                    timeout=timeout,
                    trust_env=False,
                ) as client,
                client.stream(
                    method,
                    target.value,
                    headers=headers,
                    content=content,
                ) as response,
            ):
                decoded = bytearray()
                for chunk in response.iter_bytes():
                    if response.num_bytes_downloaded > profile.max_response_bytes:
                        raise MishkanError(
                            ErrorCode.WEB,
                            "wire response exceeds the configured bound",
                            details={"limit": profile.max_response_bytes},
                        )
                    decoded.extend(chunk)
                    if len(decoded) > profile.max_decompressed_bytes:
                        raise MishkanError(
                            ErrorCode.WEB,
                            "decoded response exceeds the configured bound",
                            details={"limit": profile.max_decompressed_bytes},
                        )
                evidence = transport.backend.evidence
                if evidence is None:
                    raise MishkanError(
                        ErrorCode.WEB,
                        "connected web peer evidence is unavailable",
                    )
                return HttpExchange(
                    status_code=response.status_code,
                    headers={key.casefold(): value for key, value in response.headers.items()},
                    content=bytes(decoded),
                    wire_bytes=response.num_bytes_downloaded,
                    decoded_bytes=len(decoded),
                    connection=evidence,
                )
        except MishkanError:
            raise
        except httpx.HTTPError as exc:
            if transport.backend.failure is not None:
                raise transport.backend.failure from exc
            raise MishkanError(
                ErrorCode.WEB,
                "web transport failed",
                details={"category": type(exc).__name__},
                retryable=True,
            ) from exc
