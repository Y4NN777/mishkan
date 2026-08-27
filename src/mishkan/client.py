"""Synchronous Python SDK for the authoritative mishkand API."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from mishkan.application import ApplicationCommand, CommandResult, SnapshotEnvelope
from mishkan.artifacts import ArtifactManifest
from mishkan.daemon.auth import TokenFile
from mishkan.edits import ChangeSetResult
from mishkan.events import EventEnvelope, EventPage
from mishkan.execution import CursorRead, SessionRecord


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

    @property
    def principal_id(self) -> str:
        return self._token_file.read().principal_id

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
        entity_type: str | None = None,
        entity_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        identity_id: str | None = None,
        team_id: str | None = None,
        occurred_after: datetime | None = None,
        occurred_before: datetime | None = None,
        security_relevant: bool | None = None,
    ) -> EventPage:
        params = self._event_params(
            after=after,
            event_types=event_types,
            entity_type=entity_type,
            entity_id=entity_id,
            run_id=run_id,
            task_id=task_id,
            identity_id=identity_id,
            team_id=team_id,
            occurred_after=occurred_after,
            occurred_before=occurred_before,
            security_relevant=security_relevant,
        )
        if limit is not None:
            params = params.set("limit", limit)
        response = self._client.get("/v1/events", headers=self._headers(), params=params)
        response.raise_for_status()
        return EventPage.model_validate(response.json())

    def stream_events(
        self,
        *,
        after: int = 0,
        event_types: tuple[str, ...] = (),
        entity_type: str | None = None,
        entity_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        identity_id: str | None = None,
        team_id: str | None = None,
        occurred_after: datetime | None = None,
        occurred_before: datetime | None = None,
        security_relevant: bool | None = None,
    ) -> Iterator[EventEnvelope]:
        params = self._event_params(
            after=after,
            event_types=event_types,
            entity_type=entity_type,
            entity_id=entity_id,
            run_id=run_id,
            task_id=task_id,
            identity_id=identity_id,
            team_id=team_id,
            occurred_after=occurred_after,
            occurred_before=occurred_before,
            security_relevant=security_relevant,
        )
        with self._client.stream(
            "GET",
            "/v1/events/stream",
            headers={**self._headers(), "Last-Event-ID": str(after)},
            params=params,
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

    @staticmethod
    def _event_params(
        *,
        after: int,
        event_types: tuple[str, ...],
        entity_type: str | None,
        entity_id: str | None,
        run_id: str | None,
        task_id: str | None,
        identity_id: str | None,
        team_id: str | None,
        occurred_after: datetime | None,
        occurred_before: datetime | None,
        security_relevant: bool | None,
    ) -> httpx.QueryParams:
        params = httpx.QueryParams({"after": after})
        optional = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "run_id": run_id,
            "task_id": task_id,
            "identity_id": identity_id,
            "team_id": team_id,
            "occurred_after": occurred_after.isoformat() if occurred_after else None,
            "occurred_before": occurred_before.isoformat() if occurred_before else None,
            "security_relevant": security_relevant,
        }
        for name, value in optional.items():
            if value is not None:
                params = params.set(name, value)
        for value in event_types:
            params = params.add("event_type", value)
        return params

    def export_events_jsonl(
        self,
        destination: Path,
        *,
        after: int = 0,
        page_size: int = 1_000,
    ) -> tuple[int, int]:
        """Export a coherent event range using an atomic local replacement."""
        if page_size < 1 or page_size > 1_000:
            raise ValueError("page_size must be between 1 and 1000")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        cursor = after
        count = 0
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                while True:
                    page = self.events(after=cursor, limit=page_size)
                    for event in page.events:
                        stream.write(event.model_dump_json() + "\n")
                        count += 1
                    if not page.events:
                        break
                    cursor = page.next_cursor
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, destination)
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            Path(temporary_name).unlink(missing_ok=True)
            raise
        return count, cursor

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

    def change_sets(self, *, offset: int = 0, limit: int = 100) -> tuple[ChangeSetResult, ...]:
        response = self._client.get(
            "/v1/change-sets",
            headers=self._headers(),
            params={"offset": offset, "limit": limit},
        )
        response.raise_for_status()
        return tuple(ChangeSetResult.model_validate(item) for item in response.json())

    def change_set(self, change_set_id: str) -> ChangeSetResult:
        response = self._client.get(f"/v1/change-sets/{change_set_id}", headers=self._headers())
        response.raise_for_status()
        return ChangeSetResult.model_validate(response.json())

    def sessions(self, *, offset: int = 0, limit: int = 100) -> tuple[SessionRecord, ...]:
        response = self._client.get(
            "/v1/sessions",
            headers=self._headers(),
            params={"offset": offset, "limit": limit},
        )
        response.raise_for_status()
        return tuple(SessionRecord.model_validate(item) for item in response.json())

    def session(self, session_id: str) -> SessionRecord:
        response = self._client.get(f"/v1/sessions/{session_id}", headers=self._headers())
        response.raise_for_status()
        return SessionRecord.model_validate(response.json())

    def session_output(
        self,
        session_id: str,
        *,
        channel: str = "stdout",
        offset: int = 0,
        limit: int = 65_536,
        binary: bool = False,
    ) -> CursorRead:
        response = self._client.get(
            f"/v1/sessions/{session_id}/output",
            headers=self._headers(),
            params={
                "channel": channel,
                "offset": offset,
                "limit": limit,
                "binary": binary,
            },
        )
        response.raise_for_status()
        return CursorRead.model_validate(response.json())

    def runs(self, *, offset: int = 0, limit: int = 100) -> tuple[dict[str, object], ...]:
        response = self._client.get(
            "/v1/runs",
            headers=self._headers(),
            params={"offset": offset, "limit": limit},
        )
        response.raise_for_status()
        return tuple(dict(item) for item in response.json())

    def tasks(
        self,
        run_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[dict[str, object], ...]:
        response = self._client.get(
            f"/v1/runs/{run_id}/tasks",
            headers=self._headers(),
            params={"offset": offset, "limit": limit},
        )
        response.raise_for_status()
        return tuple(dict(item) for item in response.json())

    def mcp_connections(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[dict[str, object], ...]:
        response = self._client.get(
            "/v1/mcp/connections",
            headers=self._headers(),
            params={"offset": offset, "limit": limit},
        )
        response.raise_for_status()
        return tuple(dict(item) for item in response.json())

    def mcp_primitives(self, connection_id: str) -> tuple[dict[str, object], ...]:
        identity = quote(connection_id, safe="")
        response = self._client.get(
            f"/v1/mcp/connections/{identity}/primitives",
            headers=self._headers(),
        )
        response.raise_for_status()
        return tuple(dict(item) for item in response.json())

    def mcp_contracts(self, connection_id: str) -> tuple[dict[str, object], ...]:
        identity = quote(connection_id, safe="")
        response = self._client.get(
            f"/v1/mcp/connections/{identity}/contracts",
            headers=self._headers(),
        )
        response.raise_for_status()
        return tuple(dict(item) for item in response.json())

    def mcp_calls(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[dict[str, object], ...]:
        response = self._client.get(
            "/v1/mcp/calls",
            headers=self._headers(),
            params={"offset": offset, "limit": limit},
        )
        response.raise_for_status()
        return tuple(dict(item) for item in response.json())

    def mcp_progress(self, request_id: str, *, cursor: int = 0) -> tuple[dict[str, object], ...]:
        identity = quote(request_id, safe="")
        response = self._client.get(
            f"/v1/mcp/calls/{identity}/progress",
            headers=self._headers(),
            params={"cursor": cursor},
        )
        response.raise_for_status()
        return tuple(dict(item) for item in response.json())

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_file.read().token}"}


def daemon_url(host: str, port: int) -> str:
    formatted = f"[{host}]" if ":" in host else host
    return f"http://{formatted}:{port}"
