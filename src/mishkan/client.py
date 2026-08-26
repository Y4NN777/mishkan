"""Synchronous Python SDK for the authoritative mishkand API."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx

from mishkan.application import ApplicationCommand, CommandResult, SnapshotEnvelope
from mishkan.artifacts import ArtifactManifest
from mishkan.daemon.auth import TokenFile
from mishkan.events import EventEnvelope, EventPage


class Mishkan:
    def __init__(
        self,
        base_url: str,
        *,
        token_file: Path,
        timeout_seconds: float = 120,
    ) -> None:
        self._token_file = TokenFile(token_file)
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_seconds)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Mishkan:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def health(self) -> dict[str, str]:
        response = self._client.get("/v1/health")
        response.raise_for_status()
        return dict(response.json())

    def command(self, command: ApplicationCommand) -> CommandResult:
        response = self._client.post(
            "/v1/commands",
            headers=self._headers(),
            json=command.model_dump(mode="json"),
        )
        response.raise_for_status()
        return CommandResult.model_validate(response.json())

    def snapshot(self) -> SnapshotEnvelope:
        response = self._client.get("/v1/snapshot", headers=self._headers())
        response.raise_for_status()
        return SnapshotEnvelope.model_validate(response.json())

    def events(
        self,
        *,
        after: int = 0,
        limit: int | None = None,
        event_types: tuple[str, ...] = (),
    ) -> EventPage:
        params = httpx.QueryParams({"after": after})
        if limit is not None:
            params = params.set("limit", limit)
        for value in event_types:
            params = params.add("event_type", value)
        response = self._client.get("/v1/events", headers=self._headers(), params=params)
        response.raise_for_status()
        return EventPage.model_validate(response.json())

    def stream_events(self, *, after: int = 0) -> Iterator[EventEnvelope]:
        with self._client.stream(
            "GET",
            "/v1/events/stream",
            headers={**self._headers(), "Last-Event-ID": str(after)},
        ) as response:
            response.raise_for_status()
            data: list[str] = []
            for line in response.iter_lines():
                if not line:
                    if data:
                        yield EventEnvelope.model_validate(json.loads("\n".join(data)))
                        data.clear()
                    continue
                if line.startswith("data: "):
                    data.append(line.removeprefix("data: "))

    def artifacts(self, *, offset: int = 0, limit: int = 100) -> tuple[ArtifactManifest, ...]:
        response = self._client.get(
            "/v1/artifacts",
            headers=self._headers(),
            params={"offset": offset, "limit": limit},
        )
        response.raise_for_status()
        return tuple(ArtifactManifest.model_validate(item) for item in response.json())

    def artifact(self, reference: str) -> ArtifactManifest:
        artifact_id = reference.removeprefix("artifact:")
        response = self._client.get(f"/v1/artifacts/{artifact_id}", headers=self._headers())
        response.raise_for_status()
        return ArtifactManifest.model_validate(response.json())

    def artifact_content(self, reference: str) -> Iterator[bytes]:
        artifact_id = reference.removeprefix("artifact:")
        with self._client.stream(
            "GET", f"/v1/artifacts/{artifact_id}/content", headers=self._headers()
        ) as response:
            response.raise_for_status()
            yield from response.iter_bytes()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_file.read().token}"}


def daemon_url(host: str, port: int) -> str:
    formatted = f"[{host}]" if ":" in host else host
    return f"http://{formatted}:{port}"
