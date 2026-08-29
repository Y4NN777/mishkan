from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mishkan.config.loader import ConfigLoader
from mishkan.config.models import ProjectConfig
from mishkan.config.presets import preset_text
from mishkan.context import EngineerProfile, EngineerProfileLoader
from mishkan.daemon import DaemonBootstrap, create_app
from mishkan.daemon.auth import TokenFile


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _profile(tmp_path: Path) -> Path:
    source = tmp_path / "engineer-profile.yaml"
    source.write_text(
        """
schema_version: "1.0"
profile_id: y4nn777
revision: "1"
facts:
  - fact_id: languages
    category: development.languages
    value: Go, TypeScript, Java, Kotlin, Python, Rust, C, Swift
    confirmed_by: Y4NN777
    confirmed_at: 2026-08-29T00:00:00Z
    evidence_references: [conversation:confirmed]
""".lstrip(),
        encoding="utf-8",
    )
    return source


def test_engineer_profile_loads_only_attributed_confirmed_facts(tmp_path: Path) -> None:
    profile = EngineerProfileLoader().load(str(_profile(tmp_path)), tmp_path)

    assert profile.profile_id == "y4nn777"
    assert [fact.fact_id for fact in profile.facts] == ["languages"]
    assert profile.facts[0].confirmed_by == "Y4NN777"


@pytest.mark.anyio
async def test_daemon_exposes_configured_profile_separately_from_machine_context(
    tmp_path: Path,
) -> None:
    config_source = tmp_path / "config.yaml"
    config_source.write_text(preset_text("local"), encoding="utf-8")
    base = ConfigLoader().load([config_source]).value
    config = base.model_copy(
        update={
            "project": ProjectConfig(workspace=tmp_path),
            "engineer_profile": str(_profile(tmp_path)),
        }
    )
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read()
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/context/engineer-profile",
            headers={"Authorization": f"Bearer {token.token}"},
        )

    profile = EngineerProfile.model_validate(response.json())
    assert profile.facts[0].value.endswith("Swift")
    assert "machine" not in profile.model_dump_json().casefold()
