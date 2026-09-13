from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

from mishkan.config.loader import ConfigLoader
from mishkan.config.models import ProjectConfig
from mishkan.config.presets import preset_text
from mishkan.daemon import DaemonBootstrap
from mishkan.domain.identity import new_id
from mishkan.domain.time import utc_now
from mishkan.skills import (
    SkillActivationState,
    SkillMetadata,
    SkillMutationAction,
    SkillProvenanceLock,
    SkillSourceKind,
    SkillTrustState,
    SkillUpdateState,
    SkillVersionRecord,
    SkillVersionState,
)
from mishkan.skills.maintenance import SkillMaintenanceService
from mishkan.skills.repository import SQLiteSkillLifecycleRepository, SQLiteSkillUsageRepository


def _fingerprint(entries: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for logical_path, content in sorted(entries.items()):
        digest.update(logical_path.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
    return f"sha256:{digest.hexdigest()}"


def test_update_detection_and_stale_curation_are_non_mutating(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(preset_text("local"), encoding="utf-8")
    config = (
        ConfigLoader()
        .load([config_path])
        .value.model_copy(update={"project": ProjectConfig(workspace=tmp_path)})
    )
    paths = DaemonBootstrap().setup(config)
    assert config.skills is not None
    skill_root = tmp_path / ".mishkan" / "skills" / "active" / "source-review"
    skill_root.mkdir(parents=True)
    skill_document = b"""---
name: source-review
description: Review configured source evidence.
metadata:
  version: 0.2.0
  mishkan:
    task_classes: [software.review]
---
Review only configured source evidence.
"""
    (skill_root / "SKILL.md").write_bytes(skill_document)
    lifecycle = SQLiteSkillLifecycleRepository(paths.database)
    usage = SQLiteSkillUsageRepository(paths.database)
    now = utc_now() - timedelta(days=config.skills.stale_after_days + 1)
    fingerprint = _fingerprint({"SKILL.md": skill_document})
    stale = lifecycle.register_candidate(
        SkillVersionRecord(
            id=new_id(),
            skill_name="unused-review",
            skill_version="0.1.0",
            state=SkillVersionState.CANDIDATE,
            package_collection_id=new_id(),
            metadata=SkillMetadata(
                name="unused-review",
                description="An inactive stale review procedure.",
                version="0.1.0",
                source_id="project-test",
                source_kind=SkillSourceKind.PROJECT,
                source_revision="test:1",
                package_uri="project-test:unused-review@test:1",
                package_fingerprint=fingerprint,
                trust=SkillTrustState.TRUSTED,
                activation=SkillActivationState.CANDIDATE,
            ),
            provenance=SkillProvenanceLock(
                source_id="project-test",
                source_kind=SkillSourceKind.PROJECT,
                source_uri="project:test",
                resolved_revision="test:1",
                package_fingerprint=fingerprint,
            ),
            mutation_action=SkillMutationAction.CREATE,
            policy_fingerprint="a" * 64,
            created_at=now,
            updated_at=now,
        )
    )
    maintenance = SkillMaintenanceService(
        lifecycle,
        usage,
        sources=config.skills.sources,
        bounds=config.skills.bounds,
        project_root=paths.workspace,
        stale_after_days=config.skills.stale_after_days,
    )

    report = maintenance.updates()
    proposals = maintenance.curation()

    source = next(item for item in report.observations if item.skill_name == "source-review")
    assert source.state is SkillUpdateState.UNTRACKED
    assert source.candidate is not None
    assert proposals[0].version_id == stale.id
    assert lifecycle.get(str(stale.id)).state is SkillVersionState.CANDIDATE
