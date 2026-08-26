from __future__ import annotations

import subprocess
from pathlib import Path

from mishkan.artifacts.service import DurableArtifactService
from mishkan.edits.git import GitEffectMode, GitEffectRequest, GovernedGitService
from mishkan.persistence import SchemaManager
from mishkan.policy import Decision, EffectivePolicy, PolicyDocument, PolicyRule, PolicyScope
from mishkan.policy.models import canonical_fingerprint
from mishkan.tools.execution import EffectSettlement


def _policy(effect: str, capability: str) -> EffectivePolicy:
    document = PolicyDocument(
        source_id="test.git",
        revision="1",
        adoption_authority="test",
        rules=(
            PolicyRule(
                rule_id="git.effect",
                decision=Decision.ALLOW,
                scope=PolicyScope(capabilities=(capability,), effects=(effect,)),
            ),
        ),
    )
    return EffectivePolicy(
        documents=(document,),
        source_uris=("test:git",),
        fingerprint=canonical_fingerprint({"document": document.fingerprint}),
    )


def test_stage_and_commit_are_distinct_executable_policy_effects(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    (repository / "app.txt").write_text("content")
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    artifacts = DurableArtifactService(
        database,
        tmp_path / "artifacts",
        max_artifact_bytes=1024 * 1024,
        max_chunk_bytes=1024,
    )
    service = GovernedGitService(artifacts)

    stage = GitEffectRequest(
        mode=GitEffectMode.STAGE,
        workspace=repository,
        paths=("app.txt",),
    )
    stage_auth = service.authorization_request(
        stage,
        plan_fingerprint="a" * 64,
        identity="role:Engineer",
        repository="repo",
        role="Engineer",
    )
    staged = service.execute(
        stage,
        authorization=stage_auth,
        policy=_policy("git.stage", "git.stage"),
    )
    assert staged.settlement is EffectSettlement.COMPLETED

    commit = GitEffectRequest(
        mode=GitEffectMode.COMMIT,
        workspace=repository,
        author_name="Y4NN777",
        author_email="axel.studiesmail@gmail.com",
        message="feat: initial",
    )
    commit_auth = service.authorization_request(
        commit,
        plan_fingerprint="b" * 64,
        identity="role:Engineer",
        repository="repo",
        role="Engineer",
    )
    committed = service.execute(
        commit,
        authorization=commit_auth,
        policy=_policy("git.commit", "git.commit"),
    )
    assert committed.settlement is EffectSettlement.COMPLETED
    assert committed.after_revision is not None
    assert artifacts.read_bytes(committed.diff_reference) == b""
