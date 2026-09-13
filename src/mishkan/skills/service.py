"""Authoritative skill invocation over active pointers and immutable artifacts."""

from __future__ import annotations

from uuid import UUID

from mishkan.artifacts.service import DurableArtifactService
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.skills.catalog import select_skill_metadata, validate_skill_metadata_document
from mishkan.skills.models import (
    SkillActivationState,
    SkillBundleDefinition,
    SkillBundleMode,
    SkillInvocationEvidence,
    SkillInvocationRequest,
    SkillLoadEvidence,
    SkillMetadata,
    SkillSelection,
    SkillUsageRecord,
    SkillUseOutcome,
    SkillVersionRecord,
    SkillVersionState,
)
from mishkan.skills.repository import SQLiteSkillLifecycleRepository, SQLiteSkillUsageRepository


class SkillInvocationService:
    def __init__(
        self,
        lifecycle: SQLiteSkillLifecycleRepository,
        usage: SQLiteSkillUsageRepository,
        artifacts: DurableArtifactService,
        *,
        bundles: tuple[SkillBundleDefinition, ...],
        automatic_selection: bool,
        max_automatic_skills: int,
    ) -> None:
        self._lifecycle = lifecycle
        self._usage = usage
        self._artifacts = artifacts
        self._bundles = {bundle.bundle_id: bundle for bundle in bundles}
        self._automatic_selection = automatic_selection
        self._max_automatic_skills = max_automatic_skills

    def invoke(
        self,
        request: SkillInvocationRequest,
        *,
        policy_fingerprint: str,
    ) -> SkillInvocationEvidence:
        selections, selected_versions, limited = self._resolve(request)
        loaded: list[SkillLoadEvidence] = []
        collections: dict[str, UUID] = {}
        instruction_artifacts: dict[str, str] = {}
        for selection in selections:
            metadata = selection.selected
            version = selected_versions.get(selection.requested_name)
            if metadata is None:
                self._record_selection(selection, policy_fingerprint)
                continue
            if version is None:
                # Compatible bundle members not selected by the bundle bound, and
                # compatible automatic candidates beyond the public bound, were
                # evaluated but were neither loaded nor missed.
                continue
            collection = self._artifacts.collection(version.package_collection_id)
            instruction_reference = collection.entries.get("SKILL.md")
            if instruction_reference is None:
                raise MishkanError(ErrorCode.SKILL_CONTRACT, "active skill has no SKILL.md")
            instructions = self._artifacts.read_bytes(instruction_reference)
            instruction_fingerprint = validate_skill_metadata_document(metadata, instructions)
            evidence = SkillLoadEvidence(
                task_id=request.context.task_id,
                task_class=request.context.task_class,
                consuming_identity=request.context.consuming_identity,
                skill_name=metadata.name,
                skill_version=metadata.version,
                package_fingerprint=metadata.package_fingerprint,
                outcome=selection.outcome,
                reason=selection.reason,
                instruction_fingerprint=instruction_fingerprint,
            )
            self._usage.record(
                SkillUsageRecord(
                    task_id=request.context.task_id,
                    task_class=request.context.task_class,
                    consuming_identity=request.context.consuming_identity,
                    requested_skill=metadata.name,
                    skill_version=metadata.version,
                    package_fingerprint=metadata.package_fingerprint,
                    outcome=selection.outcome,
                    reason=selection.reason,
                    policy_fingerprint=policy_fingerprint,
                    evidence=evidence,
                )
            )
            loaded.append(evidence)
            collections[metadata.name] = collection.collection_id
            instruction_artifacts[metadata.name] = instruction_reference
        if not loaded:
            outcome = SkillUseOutcome.MISS
            reason = "no active compatible skill resolved for the invocation"
        elif limited or any(item.outcome is SkillUseOutcome.PARTIAL for item in loaded):
            outcome = SkillUseOutcome.PARTIAL
            reason = "skill invocation used a compatible fallback or configured selection bound"
        else:
            outcome = SkillUseOutcome.HIT
            reason = "active compatible skill instructions resolved from immutable artifacts"
        return SkillInvocationEvidence(
            request=request,
            outcome=outcome,
            selections=selections,
            load_evidence=tuple(loaded),
            package_collections=collections,
            instruction_artifacts=instruction_artifacts,
            reason=reason,
        )

    def _resolve(
        self,
        request: SkillInvocationRequest,
    ) -> tuple[tuple[SkillSelection, ...], dict[str, SkillVersionRecord], bool]:
        if request.requested_name is not None:
            active = self._lifecycle.active(request.requested_name)
            metadata = None if active is None else self._active_metadata(active)
            selection = select_skill_metadata(request.requested_name, metadata, request.context)
            return (selection,), ({request.requested_name: active} if active else {}), False
        if request.bundle_id is not None:
            bundle = self._bundles.get(request.bundle_id)
            if bundle is None:
                selection = select_skill_metadata(request.bundle_id, None, request.context)
                return (selection,), {}, False
            selections: list[SkillSelection] = []
            versions: dict[str, SkillVersionRecord] = {}
            for name in bundle.skills:
                active = self._lifecycle.active(name)
                metadata = None if active is None else self._active_metadata(active)
                selection = select_skill_metadata(name, metadata, request.context)
                selections.append(selection)
                if active is not None and selection.selected is not None:
                    versions[name] = active
            eligible = [item for item in selections if item.selected is not None]
            if bundle.mode is SkillBundleMode.ALL and len(eligible) != len(selections):
                return tuple(selections), {}, False
            if bundle.mode is SkillBundleMode.SELECT:
                assert bundle.max_selected is not None
                selected_names = {item.requested_name for item in eligible[: bundle.max_selected]}
                versions = {
                    name: value for name, value in versions.items() if name in selected_names
                }
            return tuple(selections), versions, False
        if not self._automatic_selection:
            selection = select_skill_metadata("automatic", None, request.context)
            return (selection,), {}, False
        active_versions = tuple(
            version
            for version in self._lifecycle.list_versions(offset=0, limit=1_000)
            if version.state is SkillVersionState.ACTIVE
            and version.metadata.task_classes
            and request.context.task_class in version.metadata.task_classes
        )
        automatic_selections = tuple(
            select_skill_metadata(
                version.skill_name,
                self._active_metadata(version),
                request.context,
            )
            for version in active_versions
        )
        automatic_eligible = tuple(
            item for item in automatic_selections if item.selected is not None
        )
        chosen = automatic_eligible[: self._max_automatic_skills]
        chosen_names = {item.requested_name for item in chosen}
        versions = {
            version.skill_name: version
            for version in active_versions
            if version.skill_name in chosen_names
        }
        if not automatic_selections:
            return (select_skill_metadata("automatic", None, request.context),), {}, False
        return automatic_selections, versions, len(automatic_eligible) > len(chosen)

    def _record_selection(self, selection: SkillSelection, policy_fingerprint: str) -> None:
        if selection.outcome is not SkillUseOutcome.MISS or selection.selected is not None:
            raise MishkanError(
                ErrorCode.SKILL_SELECTION,
                "only a proven skill miss can be persisted without load evidence",
            )
        self._usage.record(
            SkillUsageRecord(
                task_id=selection.context.task_id,
                task_class=selection.context.task_class,
                consuming_identity=selection.context.consuming_identity,
                requested_skill=selection.requested_name,
                skill_version=None,
                package_fingerprint=None,
                outcome=SkillUseOutcome.MISS,
                reason=selection.reason,
                policy_fingerprint=policy_fingerprint,
                evidence=selection,
            )
        )

    @staticmethod
    def _active_metadata(version: SkillVersionRecord) -> SkillMetadata:
        return version.metadata.model_copy(update={"activation": SkillActivationState.ACTIVE})
