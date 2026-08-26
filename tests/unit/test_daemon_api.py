from __future__ import annotations

import asyncio
import base64
import hashlib
import stat
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text

from mishkan.application import ApplicationCommand
from mishkan.config.loader import ConfigLoader
from mishkan.config.models import MishkanConfig, ProjectConfig
from mishkan.config.presets import preset_text
from mishkan.daemon import DaemonBootstrap, create_app
from mishkan.daemon.auth import TokenFile
from mishkan.daemon.bootstrap import DaemonPaths
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.persistence import SQLiteApplicationRepository


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _config(tmp_path: Path) -> MishkanConfig:
    source = tmp_path / "config.yaml"
    source.write_text(preset_text("local"), encoding="utf-8")
    loaded = ConfigLoader().load([source]).value
    return loaded.model_copy(update={"project": ProjectConfig(workspace=tmp_path)})


def test_daemon_setup_creates_current_database_and_private_token(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config, principal_id="operator-1")

    assert paths.database.is_file()
    assert stat.S_IMODE(paths.token_file.stat().st_mode) == 0o600
    assert TokenFile(paths.token_file).read().principal_id == "operator-1"


@pytest.mark.anyio
async def test_health_is_public_but_queries_require_authentication(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/v1/health")).status_code == 200
        assert (await client.get("/v1/snapshot")).status_code == 403
        token = TokenFile(paths.token_file).read().token
        response = await client.get("/v1/snapshot", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["cursor"] == 0


@pytest.mark.anyio
async def test_authenticated_command_and_event_query_share_durable_contract(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read().token
    headers = {"Authorization": f"Bearer {token}"}
    command = ApplicationCommand(
        command_type="system.checkpoint",
        actor_id="local-operator",
        target_type="system",
        target_id="local-instance",
        expected_revision=0,
        payload={"checkpoint": "daemon-api"},
    )

    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/v1/commands", headers=headers, json=command.model_dump(mode="json")
        )
        retried = await client.post(
            "/v1/commands", headers=headers, json=command.model_dump(mode="json")
        )
        events = await client.get("/v1/events", headers=headers)

    assert first.status_code == 200
    assert retried.json() == first.json()
    assert events.status_code == 200
    assert len(events.json()["events"]) == 1
    assert events.json()["events"][0]["command_id"] == str(command.command_id)


@pytest.mark.anyio
async def test_command_cannot_claim_another_actor(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read().token
    command = ApplicationCommand(
        command_type="system.checkpoint",
        actor_id="another-actor",
        target_type="system",
        payload={},
    )

    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json=command.model_dump(mode="json"),
        )

    assert response.status_code == 403
    assert response.json()["code"] == "ERR-POL-001"


@pytest.mark.anyio
async def test_artifact_upload_commands_publish_only_verified_content(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read().token
    headers = {"Authorization": f"Bearer {token}"}
    content = b"daemon artifact"
    provenance = {
        "producer_identity": "engineer",
        "run_id": "run-1",
        "task_attempt_id": "attempt-1",
        "call_id": "call-1",
        "capability": "terminal.process",
        "channel": "stdout",
    }
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        opened = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="artifact.upload.open",
                actor_id="local-operator",
                target_type="artifact_service",
                expected_revision=0,
                payload={
                    "expected_size": len(content),
                    "expected_digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
                    "media_type": "text/plain",
                    "provenance": provenance,
                },
            ).model_dump(mode="json"),
        )
        upload_id = opened.json()["payload"]["upload_id"]
        chunked = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="artifact.upload.chunk",
                actor_id="local-operator",
                target_type="artifact_upload",
                target_id=upload_id,
                expected_revision=0,
                payload={
                    "offset": 0,
                    "content_base64": base64.b64encode(content).decode(),
                },
            ).model_dump(mode="json"),
        )
        committed = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="artifact.upload.commit",
                actor_id="local-operator",
                target_type="artifact_upload",
                target_id=upload_id,
                expected_revision=1,
                payload={},
            ).model_dump(mode="json"),
        )
        artifact_id = committed.json()["payload"]["id"]
        body = await client.get(f"/v1/artifacts/{artifact_id}/content", headers=headers)

    assert opened.status_code == 200
    assert chunked.status_code == 200
    assert committed.status_code == 200
    assert body.content == content


@pytest.mark.anyio
async def test_managed_job_is_started_and_observed_through_commands(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read().token
    headers = {"Authorization": f"Bearer {token}"}
    request = {
        "mode": "job",
        "owner": "local-operator",
        "run_id": "run-1",
        "task_id": "task-1",
        "workspace": ".",
        "executable": "/bin/sh",
        "arguments": ["-c", "printf daemon-job"],
        "environment": {},
        "credential_environment": {},
        "credential_references": [],
        "profile": "standard",
        "deadline": (utc_now() + timedelta(minutes=1)).isoformat(),
        "policy_fingerprint": "a" * 64,
    }
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="session.start",
                actor_id="local-operator",
                target_type="session_service",
                payload={"request": request},
            ).model_dump(mode="json"),
        )
        session_id = started.json()["payload"]["session_id"]
        for _ in range(50):
            observed = await client.get(f"/v1/sessions/{session_id}", headers=headers)
            if observed.json()["state"] in {"settled", "failed"}:
                break
            await asyncio.sleep(0.01)
        output = await client.get(
            f"/v1/sessions/{session_id}/output",
            headers=headers,
            params={"channel": "stdout", "offset": 0},
        )

    assert started.status_code == 200
    assert observed.json()["state"] == "settled"
    assert output.json()["data"] == "daemon-job"


def test_token_rotation_invalidates_previous_credential(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token_file = TokenFile(paths.token_file)
    previous = token_file.read().token
    current = token_file.rotate().token

    assert previous != current
    assert token_file.authenticate(previous) is None
    assert token_file.authenticate(current) is not None


def test_daemon_paths_are_project_scoped(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonPaths.from_config(config)
    assert paths.database.is_relative_to(tmp_path)
    assert paths.token_file.is_relative_to(tmp_path)


def test_application_bootstrap_stays_below_ten_seconds(tmp_path: Path) -> None:
    config = _config(tmp_path)
    DaemonBootstrap().setup(config)

    started = time.perf_counter()
    application = create_app(config)

    assert application.title == "MISHKAN application API"
    assert time.perf_counter() - started < 10


@pytest.mark.anyio
async def test_crash_after_effect_reservation_refuses_implicit_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read().token
    headers = {"Authorization": f"Bearer {token}"}
    request = {
        "mode": "job",
        "owner": "local-operator",
        "run_id": "run-crash",
        "task_id": "task-crash",
        "workspace": ".",
        "executable": "/bin/sh",
        "arguments": ["-c", "sleep 10"],
        "environment": {},
        "credential_environment": {},
        "credential_references": [],
        "profile": "standard",
        "deadline": (utc_now() + timedelta(minutes=1)).isoformat(),
        "policy_fingerprint": "b" * 64,
    }
    command = ApplicationCommand(
        command_type="session.start",
        actor_id="local-operator",
        target_type="session_service",
        payload={"request": request},
    )
    original = SQLiteApplicationRepository.complete_reserved

    def crash(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        raise MishkanError(ErrorCode.RUN_INTERRUPTED, "injected crash after effect")

    monkeypatch.setattr(SQLiteApplicationRepository, "complete_reserved", crash)
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        crashed = await client.post(
            "/v1/commands", headers=headers, json=command.model_dump(mode="json")
        )
        sessions_after_crash = await client.get("/v1/sessions", headers=headers)
        monkeypatch.setattr(SQLiteApplicationRepository, "complete_reserved", original)
        replayed = await client.post(
            "/v1/commands", headers=headers, json=command.model_dump(mode="json")
        )
        sessions_after_replay = await client.get("/v1/sessions", headers=headers)
        session_id = sessions_after_crash.json()[0]["session_id"]
        cancelled = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="session.cancel",
                actor_id="local-operator",
                target_type="session",
                target_id=session_id,
                payload={},
            ).model_dump(mode="json"),
        )

    assert crashed.status_code == 400
    assert len(sessions_after_crash.json()) == 1
    assert replayed.status_code == 200
    assert replayed.json()["status"] == "refused"
    assert replayed.json()["error"]["details"]["automatic_retry"] is False
    assert len(sessions_after_replay.json()) == 1
    assert cancelled.status_code == 200


@pytest.mark.anyio
async def test_sse_removed_last_event_id_returns_explicit_snapshot_gap(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read().token
    headers = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for index in range(3):
            response = await client.post(
                "/v1/commands",
                headers=headers,
                json=ApplicationCommand(
                    command_type="system.checkpoint",
                    actor_id="local-operator",
                    target_type="system",
                    target_id=str(index),
                    payload={"index": index},
                ).model_dump(mode="json"),
            )
            assert response.status_code == 200
        with create_engine(f"sqlite:///{paths.database}").begin() as connection:
            connection.execute(text("DELETE FROM event_outbox WHERE cursor <= 2"))
        gap = await client.get(
            "/v1/events/stream",
            headers={**headers, "Last-Event-ID": "1"},
        )
        malformed = await client.get(
            "/v1/events/stream",
            headers={**headers, "Last-Event-ID": "not-a-cursor"},
        )

    assert gap.status_code == 400
    assert gap.json()["details"]["category"] == "cursor_gap"
    assert gap.json()["details"]["snapshot_required"] is True
    assert malformed.status_code == 422
