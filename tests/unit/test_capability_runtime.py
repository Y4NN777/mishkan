from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import mishkan.application.initialize as initialize_module
from mishkan.application.initialize import MishkanInitializer
from mishkan.artifacts.service import DurableArtifactService
from mishkan.browser import BrowserSupervisor
from mishkan.config.loader import ConfigLoader
from mishkan.config.models import MishkanConfig, ProjectConfig
from mishkan.config.presets import preset_text
from mishkan.daemon import DaemonBootstrap
from mishkan.planning.models import InitializationReport
from mishkan.policy import PolicyLoader
from mishkan.policy.models import EffectivePolicy
from mishkan.tools.capability_runtime import CapabilityRuntime, build_capability_runtime
from mishkan.tools.catalog import ToolCatalog
from mishkan.tools.inspection import ContentInspector, InspectionProfileLoader


def _config(tmp_path: Path) -> MishkanConfig:
    source = tmp_path / "config.yaml"
    source.write_text(preset_text("local"), encoding="utf-8")
    loaded = ConfigLoader().load([source]).value
    return loaded.model_copy(update={"project": ProjectConfig(workspace=tmp_path)})


def test_capability_runtime_binds_only_concrete_adapters_without_starting_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    assert config.artifacts is not None
    assert config.inspection_profile is not None
    artifacts = DurableArtifactService(
        paths.database,
        paths.artifacts,
        max_artifact_bytes=config.artifacts.max_artifact_bytes,
        max_chunk_bytes=config.artifacts.chunk_bytes,
    )
    inspector = ContentInspector(
        InspectionProfileLoader().load(config.inspection_profile, tmp_path)
    )
    reconciliations: list[int] = []
    original_reconcile = BrowserSupervisor.reconcile_all

    def reconcile(supervisor: BrowserSupervisor) -> int:
        reconciliations.append(1)
        return original_reconcile(supervisor)

    monkeypatch.setattr(BrowserSupervisor, "reconcile_all", reconcile)
    runtime = build_capability_runtime(
        config,
        paths.database,
        tmp_path,
        artifacts,
        inspector,
        PolicyLoader().load(config.policy_sources, tmp_path),
    )
    try:
        catalog = ToolCatalog(
            config.tool_sources,
            tmp_path,
            available_adapters=runtime.adapter_ids,
            available_dependencies=runtime.dependencies,
        )
        snapshot = catalog.snapshot(("web.complete", "browser.complete"))
        git_snapshot = catalog.snapshot(("git.complete",))

        assert {item.tool_id for item in snapshot.tools} == {
            "web.search",
            "web.map",
            "web.fetch",
            "web.request",
            "web.extract",
            "web.crawl",
            "browser.open",
            "browser.observe",
            "browser.act",
            "browser.diagnostics",
            "browser.close",
        }
        assert runtime.browser_started is False
        assert {item.tool_id for item in git_snapshot.tools} == {
            "git.stage",
            "git.commit",
            "git.push",
            "git.force_with_lease",
            "git.force_push",
        }
        assert len(reconciliations) == 1
    finally:
        runtime.close()
    assert len(reconciliations) == 2


def test_schema_13_initializer_assembles_integration_adapters_without_eager_browser_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "README.md").write_text("# Capability runtime\n", encoding="utf-8")
    for arguments in (
        ("init", "-b", "main"),
        ("config", "user.name", "Fixture"),
        ("config", "user.email", "fixture@example.invalid"),
        ("add", "."),
        ("commit", "-m", "fixture"),
    ):
        subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)
    config = _config(repository)
    observed: list[frozenset[str]] = []
    original = initialize_module.build_capability_runtime  # type: ignore[attr-defined]

    def capture(
        selected: MishkanConfig,
        database: Path,
        workspace: Path,
        artifacts: DurableArtifactService,
        inspector: ContentInspector,
        policy: EffectivePolicy,
    ) -> CapabilityRuntime:
        runtime = original(selected, database, workspace, artifacts, inspector, policy)
        observed.append(runtime.adapter_ids)
        assert runtime.browser_started is False
        return runtime

    report = InitializationReport(
        run_id="run-fixture",
        repository_id="repository-fixture",
        repository_revision="revision-fixture",
        discovery_fingerprint="discovery-fixture",
        plan_fingerprint="plan-fixture",
        resumed=False,
        completed_task_ids=(),
        results=(),
        reviews=(),
    )
    monkeypatch.setattr(initialize_module, "build_capability_runtime", capture)
    monkeypatch.setattr(
        "mishkan.crewai.flow.CrewAIInitializationFlow.kickoff",
        lambda _flow: report,
    )

    result = MishkanInitializer().run(config, repository, "Inspect capability runtime")

    assert result == report
    assert observed and "native.web.search" in observed[0]
    assert "native.browser.open" in observed[0]
    assert "native.git.force_push" in observed[0]
