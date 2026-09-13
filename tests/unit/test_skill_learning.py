from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mishkan.artifacts.service import DurableArtifactService
from mishkan.config.loader import ConfigLoader
from mishkan.config.models import MishkanConfig, ProjectConfig
from mishkan.config.presets import preset_text
from mishkan.crewai.skill_learning import CrewAISkillLearningRunner, _research_roles
from mishkan.daemon import DaemonBootstrap
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.organization.models import OrganizationDefinition, RoleDefinition
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


def _draft() -> SkillPackageDraft:
    return SkillPackageDraft(
        skill_name="python-review",
        description="Review Python changes from accepted evidence.",
        instructions_markdown="Read the accepted diff and cite supported findings.",
        required_tools=("file.read",),
        task_classes=("software.review.python",),
        retrieval_references=(),
        source_fingerprints=("e" * 64,),
        rationale="The correction is reusable and attributable.",
    )


def test_crewai_learning_runner_builds_attributed_proposal_and_independent_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CrewAISkillLearningRunner(_config(tmp_path))
    draft = _draft()
    review = SkillLearningReview(
        draft_fingerprint=draft.fingerprint,
        accepted=True,
        findings=(),
        reason="The proposal is grounded in the supplied evidence.",
    )
    calls: list[tuple[str, str, str, type[object]]] = []

    def structured(
        role: RoleDefinition,
        description: str,
        expected_output: str,
        output_model: type[object],
    ) -> object:
        calls.append((role.name, description, expected_output, output_model))
        return draft if output_model is SkillPackageDraft else review

    monkeypatch.setattr(runner, "_kickoff_structured", structured)
    request = _request().model_copy(update={"available_tools": frozenset({"file.read"})})

    proposed = runner.propose(
        request,
        desired_name="python-review",
        base=None,
        source_packet=({"locator": "inline:test", "content": "cite evidence"},),
        source_fingerprints=("e" * 64,),
    )
    evaluated = runner.review(request, proposed, source_fingerprints=("e" * 64,))

    assert proposed == draft
    assert evaluated == review
    assert [call[0] for call in calls] == ["Research_Synthesizer", "Research_Evaluator"]
    assert "Exact skill identity: python-review" in calls[0][1]
    assert 'Available tool identities: ["file.read"]' in calls[0][1]
    assert draft.fingerprint in calls[1][1]
    assert calls[0][3] is SkillPackageDraft
    assert calls[1][3] is SkillLearningReview


def test_crewai_learning_runner_supplies_existing_package_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CrewAISkillLearningRunner(_config(tmp_path))
    draft = _draft()
    descriptions: list[str] = []

    def structured(
        _role: RoleDefinition,
        description: str,
        _expected_output: str,
        _output_model: type[SkillPackageDraft],
    ) -> SkillPackageDraft:
        descriptions.append(description)
        return draft

    monkeypatch.setattr(runner, "_kickoff_structured", structured)
    base = SimpleNamespace(metadata=SimpleNamespace(model_dump=lambda **_kwargs: {"name": "base"}))

    assert (
        runner.propose(
            _request(),
            desired_name="python-review",
            base=base,  # type: ignore[arg-type]
            source_packet=({"content": "accepted evidence"},),
            source_fingerprints=("e" * 64,),
        )
        == draft
    )
    assert '"name": "base"' in descriptions[0]


def test_crewai_structured_runner_accepts_pydantic_then_raw_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CrewAISkillLearningRunner(_config(tmp_path))
    draft = _draft()

    class Models:
        @staticmethod
        def candidates_for(route: str) -> tuple[object, ...]:
            assert route == "planning"
            return (object(),)

    monkeypatch.setattr(runner, "_models", Models())
    monkeypatch.setattr(
        runner,
        "_crew",
        lambda *_args: SimpleNamespace(pydantic=draft, raw="not-json"),
    )
    role = runner._role("Research_Synthesizer")
    assert runner._kickoff_structured(role, "draft", "result", SkillPackageDraft) == draft

    monkeypatch.setattr(
        runner,
        "_crew",
        lambda *_args: SimpleNamespace(pydantic=None, raw=draft.model_dump_json()),
    )
    assert runner._kickoff_structured(role, "draft", "result", SkillPackageDraft) == draft


def test_crewai_structured_runner_retries_candidates_and_reports_failure_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path).model_copy(
        update={
            "crewai": _config(tmp_path).crewai.model_copy(update={"structured_output_retries": 1})
        }
    )
    runner = CrewAISkillLearningRunner(config)
    descriptions: list[str] = []

    class Models:
        @staticmethod
        def candidates_for(_route: str) -> tuple[object, ...]:
            return (object(), object())

    def fail(
        _role: RoleDefinition,
        _llm: object,
        description: str,
        _expected: str,
        _model: type[SkillPackageDraft],
    ) -> object:
        descriptions.append(description)
        try:
            raise ValueError("invalid structured output")
        except ValueError as cause:
            raise RuntimeError("provider failure") from cause

    monkeypatch.setattr(runner, "_models", Models())
    monkeypatch.setattr(runner, "_crew", fail)

    with pytest.raises(MishkanError) as caught:
        runner._kickoff_structured(
            runner._role("Research_Synthesizer"),
            "draft",
            "result",
            SkillPackageDraft,
        )

    assert caught.value.envelope.code is ErrorCode.REQUIRED_DEPENDENCY
    assert caught.value.envelope.retryable is True
    assert len(descriptions) == 4
    assert descriptions[0] == descriptions[2] == "draft"
    assert "prior result failed SkillPackageDraft validation" in descriptions[1]
    assert caught.value.envelope.details["failure_type_chains"][-1] == [
        "RuntimeError",
        "ValueError",
    ]


def test_crewai_learning_role_and_exception_chain_are_bounded(tmp_path: Path) -> None:
    runner = CrewAISkillLearningRunner(_config(tmp_path))
    duplicate = RoleDefinition(
        name="Research_Synthesizer",
        goal="Synthesize evidence",
        backstory="Ground every claim",
        model_route="planning",
    )
    runner._organization = OrganizationDefinition(
        schema_version="1.0",
        organization_id="invalid-test-organization",
        roles=(duplicate, duplicate),
    )

    with pytest.raises(MishkanError) as caught:
        runner._role("Research_Synthesizer")
    assert caught.value.envelope.code is ErrorCode.ROLE_CONFLICT

    error = RuntimeError("cycle")
    error.__cause__ = error
    assert runner._exception_type_chain(error) == ("RuntimeError",)
    assert len(_research_roles().roles) == 2
