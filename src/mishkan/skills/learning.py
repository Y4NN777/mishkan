"""Governed source-to-candidate skill learning application service."""

from __future__ import annotations

import hashlib
import importlib.metadata
from collections.abc import Mapping
from typing import Protocol

import yaml

from mishkan.artifacts import ArtifactManifest, ArtifactProvenance
from mishkan.artifacts.service import DurableArtifactService
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.skills.catalog import validate_skill_metadata_document
from mishkan.skills.inspection import SkillPackageInspector
from mishkan.skills.learning_repository import SQLiteSkillLearningRepository
from mishkan.skills.models import (
    SkillActivationState,
    SkillLearningRecord,
    SkillLearningRequest,
    SkillLearningReview,
    SkillLearningSourceKind,
    SkillLearningState,
    SkillMetadata,
    SkillMutationAction,
    SkillPackageDraft,
    SkillProvenanceLock,
    SkillSourceKind,
    SkillTrustState,
    SkillVersionRecord,
    SkillVersionState,
)
from mishkan.skills.repository import SQLiteSkillLifecycleRepository
from mishkan.tools.inspection import ContentInspector


class SkillLearningRunner(Protocol):
    def propose(
        self,
        request: SkillLearningRequest,
        *,
        desired_name: str,
        base: SkillVersionRecord | None,
        source_packet: tuple[dict[str, object], ...],
        source_fingerprints: tuple[str, ...],
    ) -> SkillPackageDraft: ...

    def review(
        self,
        request: SkillLearningRequest,
        draft: SkillPackageDraft,
        *,
        source_fingerprints: tuple[str, ...],
    ) -> SkillLearningReview: ...


class SkillLearningService:
    def __init__(
        self,
        repository: SQLiteSkillLearningRepository,
        lifecycle: SQLiteSkillLifecycleRepository,
        artifacts: DurableArtifactService,
        inspector: SkillPackageInspector,
        content_inspector: ContentInspector,
        runner: SkillLearningRunner,
        *,
        max_source_bytes: int,
        max_catalog_candidates: int,
    ) -> None:
        self._repository = repository
        self._lifecycle = lifecycle
        self._artifacts = artifacts
        self._inspector = inspector
        self._content_inspector = content_inspector
        self._runner = runner
        self._max_source_bytes = max_source_bytes
        self._max_catalog_candidates = max_catalog_candidates

    def learn(
        self,
        request: SkillLearningRequest,
        *,
        policy_fingerprint: str,
    ) -> SkillLearningRecord:
        source_packet, source_fingerprints, source_artifacts = self._resolve_sources(request)
        base, desired_name = self._resolve_target(request)
        record = self._repository.create(
            SkillLearningRecord(
                request=request,
                state=SkillLearningState.REQUESTED,
                source_fingerprints=source_fingerprints,
                base_version_id=None if base is None else base.id,
                policy_fingerprint=policy_fingerprint,
            )
        )
        try:
            record = self._advance(record, SkillLearningState.RESEARCHING)
            draft = self._runner.propose(
                request,
                desired_name=desired_name,
                base=base,
                source_packet=source_packet,
                source_fingerprints=source_fingerprints,
            )
            self._validate_draft(request, draft, desired_name, source_fingerprints)
            record = self._advance(record, SkillLearningState.REVIEWING, draft=draft)
            review = self._runner.review(
                request,
                draft,
                source_fingerprints=source_fingerprints,
            )
            if review.draft_fingerprint != draft.fingerprint:
                raise MishkanError(
                    ErrorCode.SKILL_TRUST,
                    "Research evaluation belongs to another skill draft",
                )
            if not review.accepted:
                return self._advance(
                    record,
                    SkillLearningState.REFUSED,
                    review=review,
                    refusal_code="research_evaluation_refused",
                )
            candidate = self._materialize_candidate(
                request,
                draft,
                base=base,
                source_artifacts=source_artifacts,
                policy_fingerprint=policy_fingerprint,
            )
            collection = self._artifacts.collection(candidate.package_collection_id)
            entries = {
                path: self._artifacts.read_bytes(reference)
                for path, reference in collection.entries.items()
            }
            registered = self._lifecycle.register_candidate(candidate)
            inspection = self._inspector.inspect_entries(
                entries,
                expected_fingerprint=registered.provenance.package_fingerprint,
            )
            inspected = self._lifecycle.record_inspection(
                str(registered.id),
                inspection,
                expected_revision=registered.revision,
            )
            return self._advance(
                record,
                SkillLearningState.PROPOSED,
                review=review,
                package_collection_id=collection.collection_id,
                candidate_version_id=inspected.id,
            )
        except MishkanError as error:
            self._mark_failed(record, error.envelope.code.value)
            raise
        except Exception:
            self._mark_failed(record, ErrorCode.REQUIRED_DEPENDENCY.value)
            raise

    def _resolve_sources(
        self,
        request: SkillLearningRequest,
    ) -> tuple[tuple[dict[str, object], ...], tuple[str, ...], tuple[str, ...]]:
        packet: list[dict[str, object]] = []
        fingerprints: list[str] = []
        source_artifacts: list[str] = []
        total = 0
        for source in request.sources:
            if source.kind is SkillLearningSourceKind.TEXT:
                assert source.content is not None
                cleaned = self._content_inspector.inspect(source.content)
                content = cleaned.encode("utf-8")
                total += len(content)
                fingerprint = f"sha256:{hashlib.sha256(content).hexdigest()}"
                packet.append(
                    {
                        "kind": source.kind.value,
                        "locator": source.locator,
                        "fingerprint": fingerprint,
                        "content": cleaned,
                    }
                )
            elif source.kind is SkillLearningSourceKind.URL:
                encoded = source.locator.encode("utf-8")
                total += len(encoded)
                fingerprint = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
                packet.append(
                    {
                        "kind": source.kind.value,
                        "locator": source.locator,
                        "fingerprint": fingerprint,
                        "content_acquired": False,
                    }
                )
            else:
                manifest = self._artifacts.manifest(source.locator)
                total += manifest.size_bytes
                raw = self._artifacts.read_bytes(source.locator)
                source_artifacts.append(source.locator)
                evidence: dict[str, object] = {
                    "kind": source.kind.value,
                    "locator": source.locator,
                    "fingerprint": manifest.digest,
                    "media_type": manifest.detected_media_type or manifest.declared_media_type,
                }
                try:
                    evidence["content"] = self._content_inspector.inspect(raw.decode("utf-8"))
                except UnicodeDecodeError:
                    evidence["content_acquired"] = False
                packet.append(evidence)
                fingerprint = manifest.digest
            if total > self._max_source_bytes:
                raise MishkanError(
                    ErrorCode.OUTPUT_CONTRACT,
                    "skill learning sources exceed the configured byte bound",
                    details={"max_source_bytes": self._max_source_bytes},
                )
            fingerprints.append(fingerprint)
        return tuple(packet), tuple(fingerprints), tuple(source_artifacts)

    def _resolve_target(
        self,
        request: SkillLearningRequest,
    ) -> tuple[SkillVersionRecord | None, str]:
        if request.suggested_name is not None:
            return self._lifecycle.active(request.suggested_name), request.suggested_name
        applicable = tuple(
            version
            for version in self._lifecycle.active_versions(limit=self._max_catalog_candidates)
            if request.task_class in version.metadata.task_classes
        )
        if len(applicable) == 1:
            return applicable[0], applicable[0].skill_name
        if applicable:
            raise MishkanError(
                ErrorCode.SKILL_SELECTION,
                "multiple active skills match the learning task; an exact skill name is required",
                details={"matching_skills": [item.skill_name for item in applicable]},
            )
        raise MishkanError(
            ErrorCode.SKILL_SELECTION,
            "a new skill proposal requires an exact suggested name",
        )

    @staticmethod
    def _validate_draft(
        request: SkillLearningRequest,
        draft: SkillPackageDraft,
        desired_name: str,
        source_fingerprints: tuple[str, ...],
    ) -> None:
        if draft.skill_name != desired_name:
            raise MishkanError(
                ErrorCode.SKILL_TRUST,
                "Research draft changed the resolved identity",
            )
        if request.task_class not in draft.task_classes:
            raise MishkanError(ErrorCode.SKILL_TRUST, "Research draft omitted its task class")
        if draft.source_fingerprints != source_fingerprints:
            raise MishkanError(ErrorCode.SKILL_TRUST, "Research draft changed its source lineage")
        expected_retrieval = {
            source.locator
            for source in request.sources
            if source.kind is SkillLearningSourceKind.URL
        }
        if set(draft.retrieval_references) != expected_retrieval:
            raise MishkanError(
                ErrorCode.SKILL_TRUST,
                "Research draft changed its attributed retrieval references",
            )
        unknown = set(draft.required_tools) - request.available_tools
        if unknown:
            raise MishkanError(
                ErrorCode.SKILL_TRUST,
                "Research draft requires tools absent from the observed task context",
                details={"unavailable_tools": sorted(unknown)},
            )

    def _materialize_candidate(
        self,
        request: SkillLearningRequest,
        draft: SkillPackageDraft,
        *,
        base: SkillVersionRecord | None,
        source_artifacts: tuple[str, ...],
        policy_fingerprint: str,
    ) -> SkillVersionRecord:
        version = "0.1.0" if base is None else self._next_patch(base.skill_version)
        source_revision = f"learning:{request.request_id}"
        metadata = SkillMetadata(
            name=draft.skill_name,
            description=draft.description,
            version=version,
            source_id="research-learning",
            source_kind=SkillSourceKind.PROJECT,
            source_revision=source_revision,
            package_uri=f"research-learning:{draft.skill_name}@{request.request_id}",
            package_fingerprint="sha256:" + "0" * 64,
            trust=SkillTrustState.UNTRUSTED,
            activation=SkillActivationState.CANDIDATE,
            author_claim="Research_Synthesizer",
            platforms=(request.platform,),
            required_tools=draft.required_tools,
            fallback_tools=draft.fallback_tools,
            organization_versions=(request.organization_version,),
            task_classes=draft.task_classes,
        )
        content = self._skill_document(metadata, draft)
        fingerprint = self._package_fingerprint({"SKILL.md": content})
        metadata = metadata.model_copy(update={"package_fingerprint": fingerprint})
        content = self._skill_document(metadata, draft)
        observed = self._package_fingerprint({"SKILL.md": content})
        if observed != fingerprint:
            # The package fingerprint is intentionally excluded from SKILL.md
            # frontmatter, so this would indicate a non-deterministic renderer.
            raise MishkanError(ErrorCode.SKILL_CONTRACT, "skill package rendering is unstable")
        validate_skill_metadata_document(metadata, content)
        manifest = self._store_skill_document(
            request,
            content,
            source_artifacts=source_artifacts,
            policy_fingerprint=policy_fingerprint,
        )
        collection = self._artifacts.create_collection({"SKILL.md": manifest.reference})
        return SkillVersionRecord(
            skill_name=draft.skill_name,
            skill_version=version,
            state=SkillVersionState.CANDIDATE,
            package_collection_id=collection.collection_id,
            metadata=metadata,
            provenance=SkillProvenanceLock(
                source_id="research-learning",
                source_kind=SkillSourceKind.PROJECT,
                source_uri=f"mishkan:skill-learning/{request.request_id}",
                resolved_revision=source_revision,
                package_fingerprint=fingerprint,
                author_claim="Research_Synthesizer",
            ),
            base_version_id=None if base is None else base.id,
            mutation_action=(
                SkillMutationAction.CREATE if base is None else SkillMutationAction.PATCH
            ),
            policy_fingerprint=policy_fingerprint,
        )

    @staticmethod
    def _skill_document(metadata: SkillMetadata, draft: SkillPackageDraft) -> bytes:
        header: dict[str, object] = {
            "name": metadata.name,
            "description": metadata.description,
            "metadata": {
                "version": metadata.version,
                "author": metadata.author_claim,
                "mishkan": {
                    "platforms": list(metadata.platforms),
                    "required_tools": list(metadata.required_tools),
                    "fallback_tools": {
                        key: list(value) for key, value in metadata.fallback_tools.items()
                    },
                    "organization_versions": list(metadata.organization_versions),
                    "task_classes": list(metadata.task_classes),
                },
            },
        }
        frontmatter = yaml.safe_dump(header, sort_keys=False, allow_unicode=True).strip()
        references = ""
        if draft.retrieval_references:
            references = "\n\n## Attributed retrieval references\n\n" + "\n".join(
                f"- {value}" for value in draft.retrieval_references
            )
        return (
            f"---\n{frontmatter}\n---\n\n{draft.instructions_markdown.strip()}{references}\n"
        ).encode()

    def _store_skill_document(
        self,
        request: SkillLearningRequest,
        content: bytes,
        *,
        source_artifacts: tuple[str, ...],
        policy_fingerprint: str,
    ) -> ArtifactManifest:
        return self._artifacts.put_bytes(
            content,
            media_type="text/markdown",
            provenance=ArtifactProvenance(
                producer_identity="Research_Synthesizer",
                run_id=f"skill-learning:{request.request_id}",
                task_attempt_id=request.task_id,
                call_id=f"skill-learning:{request.request_id}",
                capability="skill.learn",
                channel="skill-package",
                source_artifacts=source_artifacts,
                engine="CrewAI",
                engine_version=importlib.metadata.version("crewai"),
                configuration_fingerprint=policy_fingerprint,
            ),
            complete=True,
            sensitivity="internal",
            retention="skill-lineage",
        )

    @staticmethod
    def _package_fingerprint(entries: Mapping[str, bytes]) -> str:
        digest = hashlib.sha256()
        for logical_path, content in sorted(entries.items()):
            digest.update(logical_path.encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(content).digest())
        return f"sha256:{digest.hexdigest()}"

    @staticmethod
    def _next_patch(version: str) -> str:
        core = version.split("-", 1)[0].split("+", 1)[0]
        major, minor, patch = (int(value) for value in core.split("."))
        return f"{major}.{minor}.{patch + 1}"

    def _advance(
        self,
        record: SkillLearningRecord,
        state: SkillLearningState,
        **updates: object,
    ) -> SkillLearningRecord:
        updated = record.model_copy(
            update={
                "state": state,
                "revision": record.revision + 1,
                "updated_at": utc_now(),
                **updates,
            }
        )
        return self._repository.update(updated, expected_revision=record.revision)

    def _mark_failed(self, record: SkillLearningRecord, code: str) -> None:
        current = self._repository.get(str(record.request.request_id))
        if current.state in {SkillLearningState.PROPOSED, SkillLearningState.REFUSED}:
            return
        self._advance(
            current,
            SkillLearningState.FAILED,
            refusal_code=code,
        )
