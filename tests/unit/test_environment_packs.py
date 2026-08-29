from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.environment import (
    EngineeringCommandRequest,
    EngineeringCommandState,
    EnvironmentObservationRequest,
    EnvironmentObserver,
    TechnicalPackLoader,
    TechnicalPackService,
    load_environment_profile,
)


def _project(tmp_path: Path) -> None:
    files = {
        "go.mod": "module example.test/app\n",
        "package.json": '{"scripts":{"test":"node --version"}}\n',
        "pom.xml": "<project/>\n",
        "settings.gradle.kts": 'rootProject.name = "fixture"\n',
        "pyproject.toml": "[project]\nname='fixture'\nversion='0.1.0'\n",
        "Cargo.toml": '[package]\nname="fixture"\nversion="0.1.0"\n',
        "Makefile": "all:\n\t@true\n",
        "Package.swift": "// swift-tools-version: 6.0\n",
    }
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")


def test_technical_packs_resolve_exact_commands_or_truthful_unavailability(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("go", "npm", "mvn", "python3", "cargo", "make"):
        executable = binaries / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    profile = load_environment_profile(
        "package://mishkan.resources.environment/default.yaml", tmp_path
    )
    observation = EnvironmentObserver(profile).observe(
        tmp_path,
        request=EnvironmentObservationRequest(
            actor_identity="operator",
            context_id="context:multi-stack",
            repository_revision="abc123",
            execution_location="local:test",
        ),
        path_value=str(binaries),
    )
    catalogue = TechnicalPackLoader().load(
        ("package://mishkan.resources.environment/technical-packs.yaml",),
        tmp_path,
    )
    service = TechnicalPackService(catalogue)

    candidates = service.candidates(observation)
    indexed = {(item.pack_id, item.action): item for item in candidates}

    assert indexed[("go", "test")].state is EngineeringCommandState.READY
    assert indexed[("go", "test")].arguments == ("test", "./...")
    assert indexed[("javascript-typescript", "test")].engine_id == "npm"
    assert indexed[("java-kotlin", "test")].engine_id == "maven"
    assert indexed[("python", "validate")].engine_id == "python"
    assert indexed[("rust", "test")].engine_id == "rust"
    assert indexed[("c-native", "build")].engine_id == "make"
    assert indexed[("swift", "test")].state is EngineeringCommandState.UNAVAILABLE
    assert indexed[("android", "build")].state is EngineeringCommandState.UNAVAILABLE
    assert indexed[("maestro-mobile-ui", "ui-test")].state is EngineeringCommandState.UNAVAILABLE

    request = EngineeringCommandRequest(
        observation_id=observation.observation_id,
        observation_fingerprint=observation.fingerprint,
        pack_id="go",
        action="test",
        owner_identity="operator",
        run_id="run:test",
        task_id="task:test",
        session_profile="standard",
        deadline=utc_now() + timedelta(minutes=5),
        timeout_seconds=60,
    )
    plan = service.plan(observation, request)
    assert plan.execution.executable == str((binaries / "go").resolve())
    assert plan.execution.args == ("test", "./...")
    assert plan.execution.cwd == "."

    with pytest.raises(MishkanError) as stale:
        service.plan(
            observation,
            request.model_copy(update={"observation_fingerprint": "f" * 64}),
        )
    assert stale.value.envelope.code is ErrorCode.REVISION_MISMATCH


def test_pack_sources_are_configurable_and_duplicate_identity_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "packs.yaml"
    source.write_text(
        """
schema_version: "1.0"
catalogue_id: project-packs
revision: "1"
packs:
  - pack_id: project-check
    revision: "1"
    ecosystems: [python]
    commands:
      - action: check
        mode: job
        alternatives:
          - engine_id: python
            arguments: [-m, compileall, -q, .]
""".lstrip(),
        encoding="utf-8",
    )
    loader = TechnicalPackLoader()
    catalogue = loader.load((str(source),), tmp_path)
    assert catalogue.packs[0].pack_id == "project-check"

    with pytest.raises(MishkanError) as duplicate:
        loader.load((str(source), str(source)), tmp_path)
    assert duplicate.value.envelope.code is ErrorCode.CONFIGURATION
