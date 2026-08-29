"""Read-only update detection and non-destructive lifecycle curation proposals."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from mishkan.domain.errors import MishkanError
from mishkan.domain.time import utc_now
from mishkan.skills.catalog import SkillCatalog
from mishkan.skills.models import (
    SkillBounds,
    SkillCurationProposal,
    SkillMetadata,
    SkillSourceDefinition,
    SkillSourceKind,
    SkillUpdateEvidence,
    SkillUpdateReport,
    SkillUpdateState,
    SkillVersionState,
)
from mishkan.skills.repository import SQLiteSkillLifecycleRepository, SQLiteSkillUsageRepository


class SkillMaintenanceService:
    def __init__(
        self,
        lifecycle: SQLiteSkillLifecycleRepository,
        usage: SQLiteSkillUsageRepository,
        *,
        sources: tuple[SkillSourceDefinition, ...],
        bounds: SkillBounds,
        project_root: Path,
        stale_after_days: int,
    ) -> None:
        self._lifecycle = lifecycle
        self._usage = usage
        self._sources = sources
        self._bounds = bounds
        self._max_catalog_versions = bounds.max_package_files
        self._project_root = project_root
        self._stale_after_days = stale_after_days

    def updates(self) -> SkillUpdateReport:
        local_sources = tuple(
            source
            for source in self._sources
            if source.enabled and source.kind not in {SkillSourceKind.URL, SkillSourceKind.HUB}
        )
        observations: list[SkillUpdateEvidence] = [
            SkillUpdateEvidence(
                source_id=source.source_id,
                state=SkillUpdateState.ACQUISITION_REQUIRED,
                reason="remote source requires an acquired local provenance lock before inspection",
            )
            for source in self._sources
            if source.enabled and source.kind in {SkillSourceKind.URL, SkillSourceKind.HUB}
        ]
        if local_sources:
            try:
                catalogue = SkillCatalog(local_sources, self._project_root, bounds=self._bounds)
            except MishkanError as error:
                observations.append(
                    SkillUpdateEvidence(
                        source_id="configured-local-sources",
                        state=SkillUpdateState.SOURCE_UNAVAILABLE,
                        reason=error.envelope.message,
                    )
                )
            else:
                observations.extend(self._compare(item) for item in catalogue.list_metadata())
        return SkillUpdateReport(
            observations=tuple(
                sorted(
                    observations,
                    key=lambda item: (item.source_id, item.skill_name or "", item.state.value),
                )
            )
        )

    def curation(self) -> tuple[SkillCurationProposal, ...]:
        cutoff = utc_now() - timedelta(days=self._stale_after_days)
        proposals: list[SkillCurationProposal] = []
        for version in self._lifecycle.list_versions(
            offset=0,
            limit=min(self._max_catalog_versions, 1_000),
        ):
            if version.pinned or version.state in {
                SkillVersionState.ACTIVE,
                SkillVersionState.ARCHIVED,
            }:
                continue
            last_used = self._usage.last_used(version.skill_name)
            comparison = last_used or version.updated_at
            if comparison > cutoff:
                continue
            proposals.append(
                SkillCurationProposal(
                    version_id=version.id,
                    skill_name=version.skill_name,
                    skill_version=version.skill_version,
                    last_used_at=last_used,
                    stale_after_days=self._stale_after_days,
                    reason="unpinned inactive version exceeds the configured staleness threshold",
                )
            )
        return tuple(proposals)

    def _compare(self, candidate: SkillMetadata) -> SkillUpdateEvidence:
        active = self._lifecycle.active(candidate.name)
        if active is None:
            return SkillUpdateEvidence(
                source_id=candidate.source_id,
                skill_name=candidate.name,
                state=SkillUpdateState.UNTRACKED,
                candidate=candidate,
                reason="configured source package has no active durable version",
            )
        changed = tuple(
            field
            for field in type(candidate).model_fields
            if getattr(candidate, field) != getattr(active.metadata, field)
            and field not in {"activation", "trust"}
        )
        if candidate.version == active.skill_version:
            state = (
                SkillUpdateState.CURRENT
                if candidate.package_fingerprint == active.provenance.package_fingerprint
                else SkillUpdateState.VERSION_CONFLICT
            )
        else:
            state = SkillUpdateState.UPDATE_AVAILABLE
        return SkillUpdateEvidence(
            source_id=candidate.source_id,
            skill_name=candidate.name,
            state=state,
            candidate=candidate,
            active_version_id=active.id,
            active_version=active.skill_version,
            active_fingerprint=active.provenance.package_fingerprint,
            changed_fields=tuple(sorted(changed)),
            reason={
                SkillUpdateState.CURRENT: "configured source matches the active immutable package",
                SkillUpdateState.VERSION_CONFLICT: (
                    "configured source reuses the active semantic version with different content"
                ),
                SkillUpdateState.UPDATE_AVAILABLE: (
                    "configured source exposes a different version without activating it"
                ),
            }[state],
        )
