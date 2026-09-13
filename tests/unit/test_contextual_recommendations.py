from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mishkan.application import ApplicationCommand
from mishkan.config.loader import ConfigLoader
from mishkan.config.models import ProjectConfig
from mishkan.config.presets import preset_text
from mishkan.context import (
    CommunityCandidateLoader,
    ContextualRecommendation,
    ContextualRecommendationRequest,
    ContextualRecommendationService,
    RecommendationCriterion,
)
from mishkan.daemon import DaemonBootstrap, create_app
from mishkan.daemon.auth import TokenFile
from mishkan.domain.errors import MishkanError


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _catalogue(tmp_path: Path, *, suffix: str = "") -> Path:
    source = tmp_path / f"community{suffix}.yaml"
    source.write_text(
        f"""
schema_version: "1.0"
catalogue_id: community{suffix or ".base"}
revision: "1"
candidates:
  - candidate_id: tool.ready{suffix}
    kind: tool
    name: Ready tool
    revision: abc123
    source_kind: repository
    source_locator: https://example.invalid/ready
    observed_at: 2026-08-29T00:00:00Z
    evidence_references: [artifact:ready]
    constraints:
      compatibility: pass
      policy: pass
      trust: pass
      execution: pass
    criterion_values: {{quality: 0.9, cost: 0.8}}
  - candidate_id: skill.unknown{suffix}
    kind: skill
    name: Unproven skill
    revision: def456
    source_kind: hub
    source_locator: https://example.invalid/unproven
    observed_at: 2026-08-29T00:00:00Z
    evidence_references: [artifact:unproven]
    constraints:
      compatibility: pass
      policy: pass
      trust: unknown
      execution: pass
    criterion_values: {{quality: 1.0, cost: 1.0}}
""".lstrip(),
        encoding="utf-8",
    )
    return source


def _request(owner: str) -> ContextualRecommendationRequest:
    return ContextualRecommendationRequest(
        owner_identity=owner,
        context_id="mission-42",
        candidate_ids=("skill.unknown", "tool.ready", "plugin.absent"),
        criteria=(
            RecommendationCriterion(name="quality", weight=2),
            RecommendationCriterion(name="cost", weight=1),
        ),
        project_evidence=("artifact:project-profile",),
    )


def test_configured_catalogues_rank_only_fully_proven_candidates(tmp_path: Path) -> None:
    catalogues = CommunityCandidateLoader().load((str(_catalogue(tmp_path)),), tmp_path)

    result = ContextualRecommendationService(catalogues).recommend(_request("operator"))

    assert result.activation_authorized is False
    assert [item.candidate_id for item in result.ranked] == ["tool.ready"]
    assert result.ranked[0].score == pytest.approx(0.866666666667)
    excluded = {item.candidate_id: item for item in result.excluded}
    assert excluded["skill.unknown"].uncertainties == ("trust.unproven",)
    assert excluded["plugin.absent"].rejected_by == ("candidate.not_configured",)


def test_duplicate_candidate_identity_across_sources_is_refused(tmp_path: Path) -> None:
    first = _catalogue(tmp_path)
    second = _catalogue(tmp_path, suffix="-second")
    second.write_text(
        second.read_text(encoding="utf-8").replace("tool.ready-second", "tool.ready"),
        encoding="utf-8",
    )

    with pytest.raises(MishkanError, match="catalogues cannot be loaded"):
        CommunityCandidateLoader().load((str(first), str(second)), tmp_path)


@pytest.mark.anyio
async def test_daemon_recommends_but_cannot_activate_community_candidates(
    tmp_path: Path,
) -> None:
    config_source = tmp_path / "config.yaml"
    config_source.write_text(preset_text("local"), encoding="utf-8")
    base = ConfigLoader().load([config_source]).value
    config = base.model_copy(
        update={
            "project": ProjectConfig(workspace=tmp_path),
            "community_candidate_sources": (str(_catalogue(tmp_path)),),
        }
    )
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read()
    headers = {"Authorization": f"Bearer {token.token}"}
    request = _request(token.principal_id)
    command = ApplicationCommand(
        command_type="context.recommend",
        actor_id=token.principal_id,
        target_type="context_recommendation",
        target_id=str(request.request_id),
        payload={"request": request.model_dump(mode="json")},
    )
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        candidates = await client.get("/v1/context/community-candidates", headers=headers)
        response = await client.post(
            "/v1/commands", headers=headers, json=command.model_dump(mode="json")
        )

    assert candidates.json()["activation_authorized"] is False
    result = ContextualRecommendation.model_validate(response.json()["payload"])
    assert result.activation_authorized is False
    assert result.ranked[0].candidate_id == "tool.ready"
    assert "install" not in result.model_dump_json().casefold()
