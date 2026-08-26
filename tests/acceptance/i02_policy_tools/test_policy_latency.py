from time import perf_counter

from support.i02 import policy_for

from mishkan.policy import AuthorizationRequest, Decision, PolicyAuthority
from mishkan.policy.models import ResourceRequest


def test_policy_advisory_p95_is_below_500_milliseconds() -> None:
    policy = policy_for(
        "repository.read_file",
        Decision.ALLOW,
        effect_class="read",
        paths=("README.md",),
    )
    request = AuthorizationRequest(
        plan_fingerprint="a" * 64,
        identity="role:Engineer",
        objective_class="test",
        repository="test-repository",
        outcome="test.outcome",
        role="Engineer",
        capability="repository.read_file",
        effect_class="read",
        paths=("README.md",),
        resources=ResourceRequest(timeout_seconds=30, memory_mb=64),
    )
    authority = PolicyAuthority()
    durations = []
    for _sample in range(1_000):
        started = perf_counter()
        decision = authority.evaluate(request, policy)
        durations.append(perf_counter() - started)
        assert decision.decision is Decision.ALLOW

    p95 = sorted(durations)[949]
    assert p95 <= 0.5
