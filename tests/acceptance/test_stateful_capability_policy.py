from support.capabilities import policy_for

from mishkan.policy import AuthorizationRequest, Decision, PolicyAuthority
from mishkan.policy.models import ResourceRequest


def _push(branch: str) -> AuthorizationRequest:
    return AuthorizationRequest(
        plan_fingerprint="a" * 64,
        identity="role:Engineer",
        objective_class="test",
        repository="test-repository",
        outcome="test.outcome",
        role="Engineer",
        capability="git.push",
        effect_class="repository_remote_write",
        network_destinations=("github.example",),
        remotes=("origin",),
        branches=(branch,),
        credentials=("git.remote",),
        resources=ResourceRequest(
            timeout_seconds=30,
            memory_mb=512,
            network=True,
        ),
    )


def test_git_push_is_not_banned_or_granted_by_action_name() -> None:
    allow = policy_for(
        "git.push",
        Decision.ALLOW,
        effect_class="repository_remote_write",
        network_destinations=("github.example",),
        remotes=("origin",),
        branches=("develop",),
        credentials=("git.remote",),
        allow_network=True,
    )
    gated = policy_for(
        "git.push",
        Decision.REQUIRE_APPROVAL,
        effect_class="repository_remote_write",
        network_destinations=("github.example",),
        remotes=("origin",),
        branches=("main",),
        credentials=("git.remote",),
        allow_network=True,
    )
    deny = policy_for(
        "git.push",
        Decision.DENY,
        effect_class="repository_remote_write",
        network_destinations=("github.example",),
        remotes=("origin",),
        branches=("release/*",),
        credentials=("git.remote",),
        allow_network=True,
    )
    authority = PolicyAuthority()

    assert authority.evaluate(_push("develop"), allow).decision is Decision.ALLOW
    assert authority.evaluate(_push("main"), gated).decision is Decision.REQUIRE_APPROVAL
    assert authority.evaluate(_push("release/1"), deny).decision is Decision.DENY
