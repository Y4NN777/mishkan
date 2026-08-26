from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

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
