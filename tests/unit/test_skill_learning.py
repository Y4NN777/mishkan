from __future__ import annotations

from pathlib import Path

from mishkan.artifacts.service import DurableArtifactService
from mishkan.config.loader import ConfigLoader
from mishkan.config.models import MishkanConfig, ProjectConfig
from mishkan.config.presets import preset_text
from mishkan.daemon import DaemonBootstrap
from mishkan.domain.time import utc_now
from mishkan.skills import (
    SkillLearningRequest,
    SkillLearningReview,
    SkillLearningSource,
    SkillLearningSourceKind,
    SkillLearningState,
    SkillLifecycleDecision,
    SkillMutationDisposition,
    SkillPackageDraft,
    SkillVersionRecord,
    SkillVersionState,
)
from mishkan.skills.inspection import SkillInspectionProfileLoader, SkillPackageInspector
from mishkan.skills.learning import SkillLearningService
from mishkan.skills.learning_repository import SQLiteSkillLearningRepository
from mishkan.skills.repository import SQLiteSkillLifecycleRepository
from mishkan.tools.inspection import ContentInspector, InspectionProfileLoader


class _ResearchRunner:
    def __init__(self, *, accepted: bool = True) -> None:
        self.accepted = accepted
        self.base_ids: list[object] = []

    def propose(
        self,
        request: SkillLearningRequest,
        *,
        desired_name: str,
        base: SkillVersionRecord | None,
        source_packet: tuple[dict[str, object], ...],
        source_fingerprints: tuple[str, ...],
    ) -> SkillPackageDraft:
        del source_packet
        self.base_ids.append(None if base is None else base.id)
        return SkillPackageDraft(
            skill_name=desired_name,
            description="Review Python changes from accepted evidence.",
            instructions_markdown="Read the accepted diff and report only supported findings.",
            required_tools=(),
            task_classes=(request.task_class,),
            retrieval_references=(),
            source_fingerprints=source_fingerprints,
            rationale="The supplied correction is reusable for this task class.",
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
            accepted=self.accepted,
            findings=() if self.accepted else ("The correction is not sufficiently supported.",),
            reason="Independent Research evaluation completed.",
        )


def _config(tmp_path: Path) -> MishkanConfig:
    source = tmp_path / "config.yaml"
    source.write_text(preset_text("local"), encoding="utf-8")
    loaded = ConfigLoader().load([source]).value
    return loaded.model_copy(update={"project": ProjectConfig(workspace=tmp_path)})


def _services(
    tmp_path: Path,
    runner: _ResearchRunner,
) -> tuple[
    SkillLearningService,
    SQLiteSkillLifecycleRepository,
    SQLiteSkillLearningRepository,
]:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    assert config.persistence is not None
    assert config.artifacts is not None
    assert config.skills is not None
    assert config.inspection_profile is not None
    content_inspector = ContentInspector(
        InspectionProfileLoader().load(config.inspection_profile, paths.workspace)
    )
    artifacts = DurableArtifactService(
        paths.database,
        paths.artifacts,
        max_artifact_bytes=config.artifacts.max_artifact_bytes,
        max_chunk_bytes=config.artifacts.chunk_bytes,
        busy_timeout_ms=config.persistence.busy_timeout_ms,
        staging_ttl_seconds=config.artifacts.staging_ttl_seconds,
        content_inspector=content_inspector,
    )
    lifecycle = SQLiteSkillLifecycleRepository(paths.database)
    learning = SQLiteSkillLearningRepository(paths.database)
    service = SkillLearningService(
        learning,
        lifecycle,
        artifacts,
        SkillPackageInspector(
            SkillInspectionProfileLoader().load(
                config.skills.inspection_profile,
                paths.workspace,
            )
        ),
        content_inspector,
        runner,
        max_source_bytes=config.skills.learning_max_source_bytes,
        max_catalog_candidates=config.skills.bounds.max_package_files,
    )
    return service, lifecycle, learning


def _request(*, suggested_name: str | None = "python-review") -> SkillLearningRequest:
    return SkillLearningRequest(
        task_id="task-learn-1",
        task_class="software.review.python",
        consuming_identity="engineer:test",
        suggested_name=suggested_name,
        sources=(
            SkillLearningSource(
                kind=SkillLearningSourceKind.TEXT,
                locator="inline:test",
                content="Always cite the accepted Python diff before reporting a defect.",
            ),
        ),
        platform="linux",
        organization_version="org:test",
        reason="Preserve a reviewed correction.",
    )


def test_learning_creates_inspected_candidate_without_self_activation(tmp_path: Path) -> None:
    runner = _ResearchRunner()
    service, lifecycle, learning = _services(tmp_path, runner)

    result = service.learn(_request(), policy_fingerprint="a" * 64)

    assert result.state is SkillLearningState.PROPOSED
    assert result.review is not None and result.review.accepted is True
    assert result.candidate_version_id is not None
    candidate = lifecycle.get(str(result.candidate_version_id))
    assert candidate.state is SkillVersionState.ELIGIBLE
    assert candidate.metadata.activation.value == "candidate"
    assert candidate.metadata.trust.value == "untrusted"
    assert lifecycle.active("python-review") is None
    assert learning.get(str(result.request.request_id)) == result


def test_learning_updates_the_single_applicable_active_package(tmp_path: Path) -> None:
    runner = _ResearchRunner()
    service, lifecycle, _learning = _services(tmp_path, runner)
    first = service.learn(_request(), policy_fingerprint="a" * 64)
    assert first.candidate_version_id is not None
    candidate = lifecycle.get(str(first.candidate_version_id))
    active = lifecycle.decide(
        SkillLifecycleDecision(
            version_id=candidate.id,
            disposition=SkillMutationDisposition.ALLOW,
            actor_id="security-engineer",
            policy_fingerprint="b" * 64,
            reason="activate the independently inspected correction",
        )
    )

    second_request = _request(suggested_name=None).model_copy(
        update={"request_id": __import__("uuid").uuid4(), "requested_at": utc_now()}
    )
    second = service.learn(second_request, policy_fingerprint="c" * 64)

    assert runner.base_ids[-1] == active.id
    assert second.base_version_id == active.id
    assert second.candidate_version_id is not None
    updated = lifecycle.get(str(second.candidate_version_id))
    assert updated.skill_name == "python-review"
    assert updated.skill_version == "0.1.1"
    assert updated.base_version_id == active.id


def test_rejected_research_proposal_is_durable_without_candidate(tmp_path: Path) -> None:
    runner = _ResearchRunner(accepted=False)
    service, lifecycle, learning = _services(tmp_path, runner)

    result = service.learn(_request(), policy_fingerprint="d" * 64)

    assert result.state is SkillLearningState.REFUSED
    assert result.refusal_code == "research_evaluation_refused"
    assert result.candidate_version_id is None
    assert lifecycle.versions("python-review") == ()
    assert learning.get(str(result.request.request_id)) == result
