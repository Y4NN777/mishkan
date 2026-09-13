from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import httpx

from mishkan.application import ApplicationCommand
from mishkan.artifacts import ArtifactProvenance
from mishkan.artifacts.service import DurableArtifactService
from mishkan.config.loader import ConfigLoader
from mishkan.config.models import MishkanConfig, ProjectConfig
from mishkan.config.presets import preset_text
from mishkan.daemon import DaemonBootstrap, create_app
from mishkan.daemon.auth import TokenFile
from mishkan.domain.identity import new_id
from mishkan.skills import (
    SkillActivationState,
    SkillInvocationRequest,
    SkillLearningRequest,
    SkillLearningReview,
    SkillLearningSource,
    SkillLearningSourceKind,
    SkillLifecycleDecision,
    SkillMetadata,
    SkillMutationAction,
    SkillMutationDisposition,
    SkillPackageDraft,
    SkillProvenanceLock,
    SkillSelectionContext,
    SkillSourceKind,
    SkillTrustState,
    SkillVersionRecord,
    SkillVersionState,
)
from mishkan.tools.inspection import ContentInspector, InspectionProfileLoader


class _DaemonResearchRunner:
    def propose(
        self,
        request: SkillLearningRequest,
        *,
        desired_name: str,
        base: SkillVersionRecord | None,
        source_packet: tuple[dict[str, object], ...],
        source_fingerprints: tuple[str, ...],
    ) -> SkillPackageDraft:
        del base, source_packet
        return SkillPackageDraft(
            skill_name=desired_name,
            description="Apply the reviewed daemon correction.",
            instructions_markdown="Use only the accepted task evidence.",
            required_tools=(),
            task_classes=(request.task_class,),
            retrieval_references=(),
            source_fingerprints=source_fingerprints,
            rationale="The correction is reusable.",
        )

    def review(
        self,
        request: SkillLearningRequest,
        draft: SkillPackageDraft,
        *,
        source_fingerprints: tuple[str, ...],
    ) -> SkillLearningReview:
        del request, source_fingerprints
        return SkillLearningReview(
            draft_fingerprint=draft.fingerprint,
            accepted=True,
            findings=(),
            reason="Independent evaluation accepted the attributable proposal.",
        )


def _config(tmp_path: Path) -> MishkanConfig:
    source = tmp_path / "config.yaml"
    source.write_text(preset_text("local"), encoding="utf-8")
    loaded = ConfigLoader().load([source]).value
    return loaded.model_copy(update={"project": ProjectConfig(workspace=tmp_path)})


def _fingerprint(entries: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for logical_path, content in sorted(entries.items()):
        digest.update(logical_path.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
    return f"sha256:{digest.hexdigest()}"


def test_skill_candidate_and_activation_use_the_authenticated_command_path(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    assert config.persistence is not None
    assert config.artifacts is not None
    assert config.inspection_profile is not None
    inspector = ContentInspector(
        InspectionProfileLoader().load(config.inspection_profile, paths.workspace)
    )
    artifacts = DurableArtifactService(
        paths.database,
        paths.artifacts,
        max_artifact_bytes=config.artifacts.max_artifact_bytes,
        max_chunk_bytes=config.artifacts.chunk_bytes,
        busy_timeout_ms=config.persistence.busy_timeout_ms,
        staging_ttl_seconds=config.artifacts.staging_ttl_seconds,
        content_inspector=inspector,
    )
    instructions = b"""---
name: code-review
description: Review accepted software changes.
metadata:
  version: 1.0.0
---
Review only the accepted change and attach evidence.
"""
    upload = artifacts.open_upload(
        expected_size=len(instructions),
        expected_digest=f"sha256:{hashlib.sha256(instructions).hexdigest()}",
        media_type="text/markdown",
        provenance=ArtifactProvenance(
            producer_identity="research-engineer",
            run_id="run-skill-test",
            task_attempt_id="attempt-1",
            call_id="call-1",
            capability="skill.learn",
            channel="package-member",
        ),
    )
    artifacts.append_chunk(upload.upload_id, offset=0, content=instructions)
    manifest = artifacts.commit_upload(upload.upload_id)
    collection = artifacts.create_collection({"SKILL.md": manifest.reference})
    candidate = SkillVersionRecord(
        id=new_id(),
        skill_name="code-review",
        skill_version="1.0.0",
        state=SkillVersionState.CANDIDATE,
        package_collection_id=collection.collection_id,
        metadata=SkillMetadata(
            name="code-review",
            description="Review accepted software changes.",
            version="1.0.0",
            source_id="research-proposal",
            source_kind=SkillSourceKind.PROJECT,
            source_revision="artifact-collection:" + str(collection.collection_id),
            package_uri=f"research-proposal:code-review@{collection.collection_id}",
            package_fingerprint=_fingerprint({"SKILL.md": instructions}),
            trust=SkillTrustState.TRUSTED,
            activation=SkillActivationState.CANDIDATE,
        ),
        provenance=SkillProvenanceLock(
            source_id="research-proposal",
            source_kind=SkillSourceKind.PROJECT,
            source_uri="artifact:research-proposal",
            resolved_revision="artifact-collection:" + str(collection.collection_id),
            package_fingerprint=_fingerprint({"SKILL.md": instructions}),
            author_claim="research-engineer",
        ),
        mutation_action=SkillMutationAction.CREATE,
        policy_fingerprint="0" * 64,
    )
    token = TokenFile(paths.token_file).read()
    headers = {"Authorization": f"Bearer {token.token}"}

    async def scenario() -> None:
        transport = httpx.ASGITransport(
            app=create_app(config, skill_learning_runner=_DaemonResearchRunner())
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            registered = await client.post(
                "/v1/commands",
                headers=headers,
                json=ApplicationCommand(
                    command_type="skill.version.register",
                    actor_id=token.principal_id,
                    target_type="skill_version",
                    target_id=str(candidate.id),
                    expected_revision=0,
                    payload={"record": candidate.model_dump(mode="json")},
                ).model_dump(mode="json"),
            )
            assert registered.status_code == 200, registered.text
            inspected = SkillVersionRecord.model_validate(registered.json()["payload"])
            assert inspected.state is SkillVersionState.ELIGIBLE
            assert inspected.inspection is not None

            decision = SkillLifecycleDecision(
                version_id=inspected.id,
                disposition=SkillMutationDisposition.ALLOW,
                actor_id=token.principal_id,
                policy_fingerprint="0" * 64,
                reason="activate inspected project skill",
            )
            activated = await client.post(
                "/v1/commands",
                headers=headers,
                json=ApplicationCommand(
                    command_type="skill.version.decide",
                    actor_id=token.principal_id,
                    target_type="skill_version",
                    target_id=str(inspected.id),
                    expected_revision=1,
                    payload={"decision": decision.model_dump(mode="json")},
                ).model_dump(mode="json"),
            )
            assert activated.status_code == 200, activated.text
            active = await client.get("/v1/skills/code-review/active", headers=headers)
            assert active.status_code == 200
            assert active.json()["state"] == "active"
            assert active.json()["policy_fingerprint"] != "0" * 64

            invocation = SkillInvocationRequest(
                requested_name="code-review",
                context=SkillSelectionContext(
                    task_id="task-review-1",
                    task_class="software.review",
                    consuming_identity=token.principal_id,
                    platform="linux",
                    organization_version="*",
                ),
            )
            invoked = await client.post(
                "/v1/commands",
                headers=headers,
                json=ApplicationCommand(
                    command_type="skill.invoke",
                    actor_id=token.principal_id,
                    target_type="task",
                    target_id="task-review-1",
                    payload={"request": invocation.model_dump(mode="json")},
                ).model_dump(mode="json"),
            )
            assert invoked.status_code == 200, invoked.text
            invocation_evidence = invoked.json()["payload"]
            assert invocation_evidence["outcome"] == "hit"
            assert invocation_evidence["load_evidence"][0]["skill_name"] == "code-review"
            assert invocation_evidence["package_collections"] == {
                "code-review": str(collection.collection_id)
            }
            usage = await client.get(
                "/v1/skill-usage/summary",
                headers=headers,
                params={"task_class": "software.review", "skill_name": "code-review"},
            )
            assert usage.status_code == 200
            assert usage.json()["hits"] == 1
            assert usage.json()["misses"] == 0

            learning_request = SkillLearningRequest(
                task_id="task-learn-daemon",
                task_class="software.change.review",
                consuming_identity=token.principal_id,
                suggested_name="review-correction",
                sources=(
                    SkillLearningSource(
                        kind=SkillLearningSourceKind.TEXT,
                        locator="inline:test",
                        content="Always bind a review claim to accepted evidence.",
                    ),
                ),
                platform="linux",
                organization_version="org:test",
                reason="Preserve a reviewed correction.",
            )
            learned = await client.post(
                "/v1/commands",
                headers=headers,
                json=ApplicationCommand(
                    command_type="skill.learn",
                    actor_id=token.principal_id,
                    target_type="skill_learning",
                    target_id=str(learning_request.request_id),
                    payload={"request": learning_request.model_dump(mode="json")},
                ).model_dump(mode="json"),
            )
            assert learned.status_code == 200, learned.text
            assert learned.json()["payload"]["state"] == "proposed"
            assert learned.json()["payload"]["candidate_version_id"] is not None
            lineage = await client.get(
                f"/v1/skill-learning/{learning_request.request_id}",
                headers=headers,
            )
            assert lineage.status_code == 200
            assert lineage.json() == learned.json()["payload"]

    asyncio.run(scenario())
