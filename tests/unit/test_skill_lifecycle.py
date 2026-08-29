from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.identity import new_id
from mishkan.persistence import SchemaManager
from mishkan.skills import (
    SkillFindingCategory,
    SkillFindingSeverity,
    SkillInspectionProfile,
    SkillInspectionRule,
    SkillLifecycleDecision,
    SkillMutationAction,
    SkillMutationDisposition,
    SkillPackageInspector,
    SkillProvenanceLock,
    SkillSourceKind,
    SkillVersionRecord,
    SkillVersionState,
    SQLiteSkillLifecycleRepository,
)


def _package_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_file():
            content = path.read_bytes()
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(content).digest())
    return f"sha256:{digest.hexdigest()}"


def _profile(
    *, severity: SkillFindingSeverity = SkillFindingSeverity.HIGH
) -> SkillInspectionProfile:
    rules = tuple(
        SkillInspectionRule(
            rule_id=f"scan.{category.value}",
            category=category,
            severity=(
                severity
                if category is SkillFindingCategory.CREDENTIAL
                else SkillFindingSeverity.LOW
            ),
            pattern=(
                r"SECRET_[A-Z]+" if category is SkillFindingCategory.CREDENTIAL else r"NEVER_MATCH"
            ),
            summary=f"Configured {category.value} finding.",
        )
        for category in SkillFindingCategory
    )
    return SkillInspectionProfile(
        profile_id="project-skill-scan",
        revision="git:abc123",
        adoption_authority="security-engineer",
        quarantine_threshold=SkillFindingSeverity.HIGH,
        rules=rules,
        max_scanned_files=100,
        max_scanned_file_bytes=64_000,
        max_total_bytes=256_000,
    )


def _package(root: Path, *, body: str = "Follow the accepted plan.\n") -> Path:
    root.mkdir()
    (root / "SKILL.md").write_text(body, encoding="utf-8")
    return root


def _candidate(
    package: Path,
    *,
    skill_version: str = "1.0.0",
    base_version_id: object = None,
) -> SkillVersionRecord:
    fingerprint = _package_fingerprint(package)
    return SkillVersionRecord(
        skill_name="code-review",
        skill_version=skill_version,
        state=SkillVersionState.CANDIDATE,
        package_artifact_id=new_id(),
        provenance=SkillProvenanceLock(
            source_id="project-skills",
            source_kind=SkillSourceKind.PROJECT,
            source_uri="project:skills/code-review",
            resolved_revision="git:abc123",
            package_fingerprint=fingerprint,
            author_claim="research-engineer",
        ),
        base_version_id=base_version_id,
        mutation_action=(
            SkillMutationAction.CREATE if base_version_id is None else SkillMutationAction.PATCH
        ),
        policy_fingerprint="a" * 64,
    )


def _decision(
    version: SkillVersionRecord,
    disposition: SkillMutationDisposition,
    *,
    expected_active_version_id: object = None,
    quarantine_override: bool = False,
) -> SkillLifecycleDecision:
    return SkillLifecycleDecision(
        version_id=version.id,
        disposition=disposition,
        actor_id="security-engineer",
        policy_fingerprint="b" * 64,
        expected_active_version_id=expected_active_version_id,
        quarantine_override=quarantine_override,
        reason="explicit lifecycle decision",
    )


def test_configured_inspection_quarantines_without_disclosing_match(tmp_path: Path) -> None:
    package = _package(tmp_path / "skill", body="Use SECRET_CANARY only in a test.\n")
    result = SkillPackageInspector(_profile()).inspect(
        package,
        expected_fingerprint=_package_fingerprint(package),
    )

    assert result.quarantined is True
    assert len(result.findings) == 1
    assert result.findings[0].category is SkillFindingCategory.CREDENTIAL
    assert "CANARY" not in result.model_dump_json()


def test_inspection_refuses_provenance_drift_and_incomplete_profiles(tmp_path: Path) -> None:
    package = _package(tmp_path / "skill")
    with pytest.raises(MishkanError) as caught:
        SkillPackageInspector(_profile()).inspect(
            package,
            expected_fingerprint="sha256:" + "0" * 64,
        )
    assert caught.value.envelope.code is ErrorCode.SKILL_TRUST

    with pytest.raises(ValueError, match="mandatory category"):
        SkillInspectionProfile(
            profile_id="incomplete-profile",
            revision="1",
            adoption_authority="security-engineer",
            quarantine_threshold=SkillFindingSeverity.HIGH,
            rules=(_profile().rules[0],),
            max_scanned_files=1,
            max_scanned_file_bytes=1,
            max_total_bytes=1,
        )


def test_activation_is_atomic_cas_and_preserves_superseded_history(tmp_path: Path) -> None:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    repository = SQLiteSkillLifecycleRepository(database)
    package_v1 = _package(tmp_path / "v1")
    candidate_v1 = repository.register_candidate(_candidate(package_v1))
    inspected_v1 = repository.record_inspection(
        str(candidate_v1.id),
        SkillPackageInspector(_profile()).inspect(
            package_v1,
            expected_fingerprint=candidate_v1.provenance.package_fingerprint,
        ),
        expected_revision=1,
    )
    staged_v1 = repository.decide(_decision(inspected_v1, SkillMutationDisposition.REQUIRE_REVIEW))
    assert SQLiteSkillLifecycleRepository(database).get(str(staged_v1.id)).state is (
        SkillVersionState.STAGED
    )
    active_v1 = repository.decide(_decision(staged_v1, SkillMutationDisposition.ALLOW))
    assert active_v1.state is SkillVersionState.ACTIVE

    package_v2 = _package(tmp_path / "v2", body="Use the updated review procedure.\n")
    candidate_v2 = repository.register_candidate(
        _candidate(package_v2, skill_version="1.1.0", base_version_id=active_v1.id)
    )
    inspected_v2 = repository.record_inspection(
        str(candidate_v2.id),
        SkillPackageInspector(_profile()).inspect(
            package_v2,
            expected_fingerprint=candidate_v2.provenance.package_fingerprint,
        ),
        expected_revision=1,
    )
    with pytest.raises(MishkanError) as stale:
        repository.decide(_decision(inspected_v2, SkillMutationDisposition.ALLOW))
    assert stale.value.envelope.code is ErrorCode.REVISION_MISMATCH

    active_v2 = repository.decide(
        _decision(
            inspected_v2,
            SkillMutationDisposition.ALLOW,
            expected_active_version_id=active_v1.id,
        )
    )
    assert repository.active("code-review") == active_v2
    assert repository.get(str(active_v1.id)).state is SkillVersionState.SUPERSEDED
    assert len(repository.versions("code-review")) == 2


def test_staging_quarantine_override_pin_archive_and_restore_survive_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    repository = SQLiteSkillLifecycleRepository(database)
    package = _package(tmp_path / "skill", body="Check SECRET_CANARY.\n")
    candidate = repository.register_candidate(_candidate(package))
    quarantined = repository.record_inspection(
        str(candidate.id),
        SkillPackageInspector(_profile()).inspect(
            package,
            expected_fingerprint=candidate.provenance.package_fingerprint,
        ),
        expected_revision=1,
    )
    still_quarantined = repository.decide(
        _decision(quarantined, SkillMutationDisposition.REQUIRE_REVIEW)
    )
    assert still_quarantined.state is SkillVersionState.QUARANTINED

    active = SQLiteSkillLifecycleRepository(database).decide(
        _decision(
            still_quarantined,
            SkillMutationDisposition.ALLOW,
            quarantine_override=True,
        )
    )
    pinned = repository.set_pin(str(active.id), pinned=True, expected_revision=active.revision)
    with pytest.raises(MishkanError, match="pinned"):
        repository.archive(
            str(pinned.id),
            _decision(pinned, SkillMutationDisposition.ALLOW),
            expected_revision=pinned.revision,
        )
    unpinned = repository.set_pin(str(pinned.id), pinned=False, expected_revision=pinned.revision)
    archived = repository.archive(
        str(unpinned.id),
        _decision(unpinned, SkillMutationDisposition.ALLOW),
        expected_revision=unpinned.revision,
    )
    assert repository.active("code-review") is None

    restored = SQLiteSkillLifecycleRepository(database).decide(
        _decision(
            archived,
            SkillMutationDisposition.ALLOW,
            quarantine_override=True,
        )
    )
    assert restored.state is SkillVersionState.ACTIVE
    assert restored.provenance.package_fingerprint == candidate.provenance.package_fingerprint
