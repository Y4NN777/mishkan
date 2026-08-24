from __future__ import annotations

from pathlib import Path

from support.i02 import RecordingAdapter, context_for, inspector, policy_for

from mishkan.policy import Decision, PolicyAuthority
from mishkan.tools.adapters import WriteFileAdapter
from mishkan.tools.gateway import CapabilityGateway, MappingCredentialResolver, MemoryEvidenceSink
from mishkan.tools.gateway_models import AdapterResult, CallStatus, DeclaredTargets, ResolvedTargets


def test_authorized_write_uses_exact_binding_and_returns_reviewable_diff(tmp_path: Path) -> None:
    target = tmp_path / "README.md"
    target.write_text("before\n", encoding="utf-8")
    policy = policy_for(
        "repository.write_file",
        Decision.ALLOW,
        effect_class="filesystem_write",
        paths=("README.md",),
    )
    context = context_for(tmp_path, "repository.write_file", policy, ("README.md",))
    evidence = MemoryEvidenceSink()
    gateway = CapabilityGateway(
        tmp_path,
        PolicyAuthority(),
        MappingCredentialResolver({}),
        inspector(tmp_path),
        {"native.repository.write_file": WriteFileAdapter()},
        evidence,
    )

    result = gateway.invoke(
        context,
        {"path": "README.md", "content": "after\n"},
        DeclaredTargets(paths=("README.md",)),
    )

    assert result.status is CallStatus.COMPLETED
    assert target.read_text(encoding="utf-8") == "after\n"
    assert result.output is not None and "-before" in result.output["diff"]
    assert result.output is not None and "+after" in result.output["diff"]
    assert [event.event_type for event in evidence.events] == [
        "tool.call_authorized",
        "tool.call_completed",
    ]


def test_policy_denial_prevents_the_same_write_adapter_effect(tmp_path: Path) -> None:
    target = tmp_path / "README.md"
    target.write_text("unchanged\n", encoding="utf-8")
    policy = policy_for(
        "repository.write_file",
        Decision.DENY,
        effect_class="filesystem_write",
        paths=("README.md",),
    )
    context = context_for(tmp_path, "repository.write_file", policy, ("README.md",))
    adapter = RecordingAdapter(AdapterResult(output={}, actual_targets=ResolvedTargets()))
    evidence = MemoryEvidenceSink()
    gateway = CapabilityGateway(
        tmp_path,
        PolicyAuthority(),
        MappingCredentialResolver({}),
        inspector(tmp_path),
        {"native.repository.write_file": adapter},
        evidence,
    )

    result = gateway.invoke(
        context,
        {"path": "README.md", "content": "changed\n"},
        DeclaredTargets(paths=("README.md",)),
    )

    assert result.status is CallStatus.REFUSED
    assert result.error_code == "ERR-POL-001"
    assert adapter.calls == 0
    assert target.read_text(encoding="utf-8") == "unchanged\n"
    assert evidence.events[-1].decision == "refused"


def test_invalid_input_is_refused_before_late_credentials_or_dispatch(tmp_path: Path) -> None:
    policy = policy_for(
        "git.push",
        Decision.ALLOW,
        effect_class="repository_remote_write",
        network_destinations=("github.example",),
        remotes=("origin",),
        branches=("develop",),
        credentials=("git.remote",),
        allow_network=True,
    )
    context = context_for(
        tmp_path,
        "git.push",
        policy,
        ("test-repository", "origin", "develop", "github.example"),
        network=True,
    )
    credentials = MappingCredentialResolver({"git.remote": "credential-canary"})
    adapter = RecordingAdapter(AdapterResult(output={}, actual_targets=ResolvedTargets()))
    gateway = CapabilityGateway(
        tmp_path,
        PolicyAuthority(),
        credentials,
        inspector(tmp_path),
        {"native.git.push": adapter},
        MemoryEvidenceSink(),
    )

    result = gateway.invoke(
        context,
        {"repository": "test-repository"},
        DeclaredTargets(repositories=("test-repository",)),
    )

    assert result.status is CallStatus.REFUSED
    assert result.error_code == "ERR-TOL-003"
    assert credentials.calls == 0
    assert adapter.calls == 0


def test_uncertain_stateful_effect_is_never_automatically_repeated(tmp_path: Path) -> None:
    policy = policy_for(
        "repository.write_file",
        Decision.ALLOW,
        effect_class="filesystem_write",
        paths=("README.md",),
    )
    context = context_for(tmp_path, "repository.write_file", policy, ("README.md",))
    adapter = RecordingAdapter(TimeoutError("effect outcome unknown"))
    gateway = CapabilityGateway(
        tmp_path,
        PolicyAuthority(),
        MappingCredentialResolver({}),
        inspector(tmp_path),
        {"native.repository.write_file": adapter},
        MemoryEvidenceSink(),
    )

    result = gateway.invoke(
        context,
        {"path": "README.md", "content": "after\n"},
        DeclaredTargets(paths=("README.md",)),
    )

    assert result.status is CallStatus.UNCERTAIN
    assert result.retryable is False
    assert adapter.calls == 1


def test_invalid_adapter_output_is_contained_after_dispatch(tmp_path: Path) -> None:
    policy = policy_for(
        "repository.write_file",
        Decision.ALLOW,
        effect_class="filesystem_write",
        paths=("README.md",),
    )
    context = context_for(tmp_path, "repository.write_file", policy, ("README.md",))
    adapter = RecordingAdapter(
        AdapterResult(output={"unexpected": True}, actual_targets=ResolvedTargets())
    )
    gateway = CapabilityGateway(
        tmp_path,
        PolicyAuthority(),
        MappingCredentialResolver({}),
        inspector(tmp_path),
        {"native.repository.write_file": adapter},
        MemoryEvidenceSink(),
    )

    result = gateway.invoke(
        context,
        {"path": "README.md", "content": "after\n"},
        DeclaredTargets(paths=("README.md",)),
    )

    assert adapter.calls == 1
    assert result.status is CallStatus.FAILED
    assert result.error_code == "ERR-TOL-003"
    assert result.output is None
