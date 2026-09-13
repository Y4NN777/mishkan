from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from mishkan.client import Mishkan
from mishkan.daemon.auth import TokenFile
from mishkan.events import EventEnvelope, EventPage


def _event(cursor: int) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        cursor=cursor,
        event_type="system.checkpoint_recorded",
        source="mishkand",
        entity_type="system",
        entity_id="local-instance",
        occurred_at=datetime.now(UTC),
        sensitivity="internal",
        payload={"cursor": cursor},
    )


def test_event_export_is_paginated_atomic_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "token.json"
    TokenFile(token_file).create("operator")
    client = Mishkan("http://127.0.0.1:8888", token_file=token_file)

    def events(*, after: int = 0, limit: int | None = None, event_types=()):  # type: ignore[no-untyped-def]
        del limit, event_types
        values = tuple(_event(cursor) for cursor in range(after + 1, min(after + 3, 4)))
        return EventPage(
            after_cursor=after,
            next_cursor=values[-1].cursor if values else after,
            retained_from_cursor=1,
            events=values,
        )

    monkeypatch.setattr(client, "events", events)
    output = tmp_path / "exports" / "events.jsonl"
    count, cursor = client.export_events_jsonl(output, page_size=2)
    client.close()

    documents = [json.loads(line) for line in output.read_text().splitlines()]
    assert count == 3
    assert cursor == 3
    assert [document["cursor"] for document in documents] == [1, 2, 3]
    assert not tuple(output.parent.glob("*.tmp"))


def test_event_export_rejects_unbounded_page_size(tmp_path: Path) -> None:
    token_file = tmp_path / "token.json"
    TokenFile(token_file).create("operator")
    with (
        Mishkan("http://127.0.0.1:8888", token_file=token_file) as client,
        pytest.raises(ValueError),
    ):
        client.export_events_jsonl(tmp_path / "events.jsonl", page_size=1001)


def test_sdk_bounded_query_surfaces_use_authenticated_versioned_routes(tmp_path: Path) -> None:
    token_file = tmp_path / "token.json"
    credential = TokenFile(token_file).create("operator")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/context/community-candidates":
            return httpx.Response(200, json={"candidates": []})
        if request.url.path.endswith("/active"):
            return httpx.Response(200, content=b"null")
        return httpx.Response(200, json=[])

    with Mishkan("http://mishkand.test/", token_file=token_file) as client:
        client._client.close()
        client._client = httpx.Client(
            base_url="http://mishkand.test",
            transport=httpx.MockTransport(handler),
        )
        assert client.principal_id == "operator"
        assert client.health() == {"status": "ok"}
        assert client.community_candidates() == ()
        assert client.artifacts(offset=2, limit=3) == ()
        assert client.artifact_collections(offset=2, limit=3) == ()
        assert client.artifact_references(offset=2, limit=3) == ()
        assert client.artifact_holds(offset=2, limit=3) == ()
        assert client.artifact_pins(offset=2, limit=3) == ()
        assert client.change_sets(offset=2, limit=3) == ()
        assert client.sessions(offset=2, limit=3) == ()
        assert client.runs(offset=2, limit=3) == ()
        assert client.tasks("run/id", offset=2, limit=3) == ()
        assert client.skills(offset=2, limit=3, name="review") == ()
        assert client.active_skill("review/name") is None
        assert client.skill_learning(offset=2, limit=3) == ()
        assert client.skill_curation() == ()
        assert client.mcp_connections(offset=2, limit=3) == ()
        assert client.mcp_primitives("connection/id") == ()
        assert client.mcp_contracts("connection/id") == ()
        assert client.mcp_calls(offset=2, limit=3) == ()
        assert client.mcp_progress("request/id", cursor=4) == ()

    authenticated = [request for request in requests if request.url.path != "/v1/health"]
    assert authenticated
    assert all(
        request.headers["Authorization"] == f"Bearer {credential.token}"
        for request in authenticated
    )
    paths = {request.url.raw_path.decode() for request in requests}
    assert "/v1/skills/review%2Fname/active" in paths
    assert "/v1/runs/run/id/tasks?offset=2&limit=3" in paths
    assert "/v1/mcp/connections/connection%2Fid/primitives" in paths
    assert "/v1/mcp/calls/request%2Fid/progress?cursor=4" in paths
