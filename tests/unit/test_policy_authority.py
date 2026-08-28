from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.policy import (
    ApprovalEvidence,
    AuthorizationRequest,
    Decision,
    EffectivePolicy,
    PolicyAuthority,
    PolicyDocument,
    PolicyLoader,
    PolicyRule,
    PolicyScope,
    ResourceRequest,
)
from mishkan.policy.models import canonical_fingerprint


def _request(**changes: object) -> AuthorizationRequest:
    values: dict[str, object] = {
        "plan_fingerprint": "a" * 64,
        "identity": "role:Engineer",
        "objective_class": "delivery",
        "repository": "repo-1",
        "outcome": "release.candidate",
        "role": "Engineer",
        "capability": "git.push",
        "effect_class": "repository_write",
        "remotes": ("origin",),
        "branches": ("develop",),
        "resources": ResourceRequest(timeout_seconds=30),
    }
    values.update(changes)
    return AuthorizationRequest.model_validate(values)


def _document(*rules: PolicyRule, revision: str = "1") -> PolicyDocument:
    return PolicyDocument(
        source_id="project.policy",
        revision=revision,
        adoption_authority="Engineer",
        rules=rules,
    )


def _policy(*rules: PolicyRule) -> EffectivePolicy:
    document = _document(*rules)
    payload = {"uri": "project:policy.yaml", "fingerprint": document.fingerprint}
    return EffectivePolicy(
        documents=(document,),
        source_uris=("project:policy.yaml",),
        fingerprint=canonical_fingerprint(payload),
    )


def _rule(rule_id: str, decision: Decision, **scope: object) -> PolicyRule:
    return PolicyRule(
        rule_id=rule_id,
        decision=decision,
        scope=PolicyScope.model_validate(scope),
    )


def test_same_stateful_capability_is_allowed_gated_or_denied_by_public_scope() -> None:
    authority = PolicyAuthority()
    allow_policy = _policy(
        _rule(
            "push.develop",
            Decision.ALLOW,
            capabilities=("git.push",),
            branches=("develop",),
        )
    )
    approval_policy = _policy(
        _rule(
            "push.main",
            Decision.REQUIRE_APPROVAL,
            capabilities=("git.push",),
            branches=("main",),
        )
    )
    deny_policy = _policy(
        _rule(
            "push.release",
            Decision.DENY,
            capabilities=("git.push",),
            branches=("release/*",),
        )
    )

    assert authority.evaluate(_request(), allow_policy).decision is Decision.ALLOW
    assert (
        authority.evaluate(_request(branches=("main",)), approval_policy).decision
        is Decision.REQUIRE_APPROVAL
    )
    assert (
        authority.evaluate(_request(branches=("release/1",)), deny_policy).decision is Decision.DENY
    )
    assert authority.evaluate(_request(branches=("other",)), allow_policy).decision is Decision.DENY


def test_equally_ranked_conflicting_rules_fail_closed() -> None:
    policy = _policy(
        _rule("push.allow", Decision.ALLOW, capabilities=("git.push",)),
        _rule("push.deny", Decision.DENY, capabilities=("git.push",)),
    )

    with pytest.raises(MishkanError) as caught:
        PolicyAuthority().evaluate(_request(), policy)

    assert caught.value.envelope.code is ErrorCode.POLICY_CONFLICT
    assert caught.value.envelope.details["decisions"] == ["allow", "deny"]


def test_approval_is_exact_expiring_and_revocable() -> None:
    request = _request(branches=("main",))
    policy = _policy(
        _rule(
            "push.main",
            Decision.REQUIRE_APPROVAL,
            capabilities=("git.push",),
            branches=("main",),
        )
    )
    approval = ApprovalEvidence(
        request_fingerprint=request.fingerprint,
        plan_fingerprint=request.plan_fingerprint,
        policy_fingerprint=policy.fingerprint,
        approved_by="engineer:Y4NN777",
        expires_at=utc_now() + timedelta(minutes=10),
        reason="approved main promotion",
    )

    decision = PolicyAuthority().evaluate(request, policy, approval)
    assert decision.decision is Decision.ALLOW
    assert decision.approval_id == str(approval.id)

    revoked = approval.model_copy(update={"revoked_at": utc_now()})
    with pytest.raises(MishkanError) as caught:
        PolicyAuthority().evaluate(request, policy, revoked)
    assert caught.value.envelope.code is ErrorCode.AUTHORIZATION_MISSING


def test_policy_revision_changes_effective_fingerprint(tmp_path: Path) -> None:
    first = tmp_path / "policy-1.yaml"
    second = tmp_path / "policy-2.yaml"
    template = """schema_version: '1.0'
source_id: project
revision: '{revision}'
adoption_authority: Engineer
rules:
  - rule_id: read
    decision: allow
    priority: 0
    scope:
      capabilities: [repository.read_file]
"""
    first.write_text(template.format(revision="1"), encoding="utf-8")
    second.write_text(template.format(revision="2"), encoding="utf-8")

    loader = PolicyLoader()
    first_policy = loader.load((str(first),), tmp_path)
    second_policy = loader.load((str(second),), tmp_path)

    assert first_policy.fingerprint != second_policy.fingerprint
    assert first_policy.documents[0].adoption_authority == "Engineer"


@pytest.mark.parametrize(
    ("effective_from", "retired_at"),
    [
        (utc_now() + timedelta(minutes=10), None),
        (None, utc_now() - timedelta(minutes=10)),
    ],
)
def test_inactive_policy_revision_cannot_grant_authority(
    effective_from: datetime | None,
    retired_at: datetime | None,
) -> None:
    document = PolicyDocument(
        source_id="project.policy",
        revision="inactive",
        adoption_authority="Engineer",
        effective_from=effective_from,
        retired_at=retired_at,
        rules=(_rule("push.inactive", Decision.ALLOW, capabilities=("git.push",)),),
    )
    policy = EffectivePolicy(
        documents=(document,),
        source_uris=("project:inactive.yaml",),
        fingerprint=canonical_fingerprint(
            {"uri": "project:inactive.yaml", "fingerprint": document.fingerprint}
        ),
    )

    decision = PolicyAuthority().evaluate(_request(), policy)

    assert decision.decision is Decision.DENY
    assert decision.matched_rule_ids == ()


def test_bundled_policy_is_loaded_through_a_public_package_uri(tmp_path: Path) -> None:
    policy = PolicyLoader().load(
        ("package://mishkan.resources.policies/local-control-plane.yaml",),
        tmp_path,
    )

    assert policy.documents[0].source_id == "bundled.local"
    assert policy.source_uris == (
        "package://mishkan.resources.policies/local-control-plane.yaml",
    )


@pytest.mark.parametrize(
    "git_executable",
    (
        "/usr/bin/git",
        "/usr/local/Cellar/git/2.52.0/bin/git",
        "/opt/homebrew/Cellar/git/2.52.0/bin/git",
        "/Applications/Xcode_26.3.app/Contents/Developer/usr/bin/git",
        "/Library/Developer/CommandLineTools/usr/bin/git",
    ),
)
def test_bundled_init_policy_allows_portable_safe_git_read_and_gates_other_host_commands(
    tmp_path: Path,
    git_executable: str,
) -> None:
    policy = PolicyLoader().load(
        ("package://mishkan.resources.policies/local-control-plane.yaml",),
        tmp_path,
    )
    base = {
        "identity": "role:Repository_Investigator",
        "objective_class": "repository-initialization",
        "repository": "fixture-repository",
        "outcome": "mishkan.init",
        "role": "Repository_Investigator",
        "capability": "core.process.exec",
        "effect_class": "command",
        "paths": (".",),
        "resources": ResourceRequest(timeout_seconds=30, network=True),
    }
    git_read = _request(
        **base,
        executables=(git_executable,),
        arguments=("show", "HEAD:README.md"),
    )
    recursive_init = _request(
        **base,
        executables=("/usr/bin/mishkan",),
        arguments=("init",),
    )
    allowed = PolicyAuthority().evaluate(git_read, policy)
    gated = PolicyAuthority().evaluate(recursive_init, policy)

    assert allowed.decision is Decision.ALLOW
    assert allowed.matched_rule_ids == ("local.repository-probe.git-read",)
    assert gated.decision is Decision.REQUIRE_APPROVAL
    assert gated.matched_rule_ids == ("local.repository-probe.approval",)


def test_unicode_confusable_is_rejected_in_authorization_identity() -> None:
    with pytest.raises(ValueError, match="stable visible Unicode"):
        _request(identity="role:\uff25ngineer")
