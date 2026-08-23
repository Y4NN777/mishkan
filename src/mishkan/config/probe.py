"""Warning-only connectivity probes for configured endpoints."""

from collections.abc import Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict

from mishkan.config.models import MishkanConfig


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str
    endpoint: str
    reachable: bool
    warning: str | None = None


Probe = Callable[[str, float], None]


def http_probe(endpoint: str, timeout: float) -> None:
    request = Request(endpoint, method="GET")
    try:
        with urlopen(request, timeout=timeout):
            return
    except (OSError, URLError) as exc:
        raise ConnectionError(type(exc).__name__) from exc


def probe_connections(
    config: MishkanConfig,
    *,
    timeout: float = 1.0,
    probe: Probe = http_probe,
) -> tuple[ProbeResult, ...]:
    targets: list[tuple[str, str]] = []
    targets.extend(
        (f"provider:{name}", str(provider.probe_url or provider.endpoint))
        for name, provider in sorted(config.providers.items())
    )
    targets.extend(
        (f"service:{name}", str(service.probe_url or service.endpoint))
        for name, service in sorted(config.services.items())
    )

    results: list[ProbeResult] = []
    for target, endpoint in targets:
        try:
            probe(endpoint, timeout)
        except ConnectionError as exc:
            results.append(
                ProbeResult(
                    target=target,
                    endpoint=endpoint,
                    reachable=False,
                    warning=f"connection probe failed: {exc}",
                )
            )
        else:
            results.append(ProbeResult(target=target, endpoint=endpoint, reachable=True))
    return tuple(results)
