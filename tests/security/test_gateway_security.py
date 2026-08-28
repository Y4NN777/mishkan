from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from support.capabilities import RecordingAdapter, context_for, inspector, policy_for

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.policy import Decision, PolicyAuthority
from mishkan.tools.adapters import ContainerCommandAdapter
from mishkan.tools.catalog import ToolCatalog
from mishkan.tools.gateway import CapabilityGateway, MappingCredentialResolver, MemoryEvidenceSink
from mishkan.tools.gateway_models import AdapterResult, CallStatus, DeclaredTargets, ResolvedTargets
from mishkan.tools.isolation import ContainerCommand, IsolationProfileLoader


@pytest.mark.paths
def test_path_traversal_is_refused_before_dispatch(tmp_path: Path) -> None:
    policy = policy_for(
        "repository.write_file",
        Decision.ALLOW,
        effect_class="filesystem_write",
        paths=("*",),
    )
    context = context_for(tmp_path, "repository.write_file", policy, ("*",))
    adapter = RecordingAdapter(AdapterResult(output={}, actual_targets=ResolvedTargets()))
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
        {"path": "../outside.txt", "content": "blocked"},
        DeclaredTargets(paths=("../outside.txt",)),
    )

    assert result.status is CallStatus.REFUSED
    assert result.error_code == "ERR-POL-001"
    assert adapter.calls == 0


@pytest.mark.symlinks
def test_symlink_escape_is_refused_before_dispatch(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-scope.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    policy = policy_for(
        "repository.write_file",
        Decision.ALLOW,
        effect_class="filesystem_write",
        paths=("link.txt",),
    )
    context = context_for(tmp_path, "repository.write_file", policy, ("link.txt",))
    adapter = RecordingAdapter(AdapterResult(output={}, actual_targets=ResolvedTargets()))
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
        {"path": "link.txt", "content": "blocked"},
        DeclaredTargets(paths=("link.txt",)),
    )

    assert result.status is CallStatus.REFUSED
    assert adapter.calls == 0
    assert outside.read_text(encoding="utf-8") == "outside"


@pytest.mark.symlinks
def test_project_scoped_tool_source_cannot_escape_through_a_symlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-catalog.yaml"
    outside.write_text("schema_version: '1.0'\n", encoding="utf-8")
    (tmp_path / "catalog.yaml").symlink_to(outside)

    with pytest.raises(MishkanError) as caught:
        ToolCatalog(("project:catalog.yaml",), tmp_path)

    assert caught.value.envelope.code is ErrorCode.AUTHORITY_NOT_GRANTED


@pytest.mark.secrets
def test_resolved_credential_never_enters_result_or_audit_evidence(tmp_path: Path) -> None:
    secret = "credential-canary-123456789"
    policy = policy_for(
        "git.push",
        Decision.ALLOW,
        effect_class="repository_remote_write",
        remotes=("origin",),
        branches=("develop",),
        credentials=("git.remote",),
        allow_network=True,
    )
    context = context_for(
        tmp_path,
        "git.push",
        policy,
        ("origin", "develop"),
        network=True,
    )
    adapter = RecordingAdapter(
        AdapterResult(
            output={
                "remote": "origin",
                "branch": "develop",
                "revision": "abc1234",
                "external_reference": f"https://github.example/result?token={secret}",
            },
            actual_targets=ResolvedTargets(
                remotes=("origin",),
                branches=("develop",),
            ),
        )
    )
    evidence = MemoryEvidenceSink()
    gateway = CapabilityGateway(
        tmp_path,
        PolicyAuthority(),
        MappingCredentialResolver({"git.remote": secret}),
        inspector(tmp_path),
        {"native.git.push": adapter},
        evidence,
    )

    result = gateway.invoke(
        context,
        {
            "remote": "origin",
            "branch": "develop",
            "expected_remote_url": "https://github.example/repository.git",
            "expected_head": "abc1234",
        },
        DeclaredTargets(
            remotes=("origin",),
            branches=("develop",),
        ),
    )

    assert result.status is CallStatus.FAILED
    assert result.error_code == "ERR-SEC-001"
    serialized = json.dumps(
        {
            "result": result.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in evidence.events],
        }
    )
    assert secret not in serialized
    assert adapter.last_credentials == {"git.remote": secret}


class RecordingRunner:
    def __init__(self) -> None:
        self.argv: tuple[str, ...] | None = None
        self.timeout: int | None = None

    def run(self, argv: tuple[str, ...], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        self.argv = argv
        self.timeout = timeout_seconds
        return subprocess.CompletedProcess(argv, 0, "command output", "")


@pytest.mark.commands
def test_command_is_governed_by_public_isolation_values_not_a_private_action_list(
    tmp_path: Path,
) -> None:
    profile = IsolationProfileLoader().load(
        "package://mishkan.resources.isolation/local-no-network.yaml",
        tmp_path,
    )
    runner = RecordingRunner()
    adapter = ContainerCommandAdapter(ContainerCommand(profile, runner))
    policy = policy_for(
        "command.run",
        Decision.ALLOW,
        effect_class="command",
        paths=(".",),
        executables=("git",),
    )
    context = context_for(
        tmp_path,
        "command.run",
        policy,
        (".", "git"),
        runtime="container",
        isolation_profile="local.no-network",
    )
    gateway = CapabilityGateway(
        tmp_path,
        PolicyAuthority(),
        MappingCredentialResolver({}),
        inspector(tmp_path),
        {"isolation.command": adapter},
        MemoryEvidenceSink(),
    )

    result = gateway.invoke(
        context,
        {"argv": ["git", "push", "origin", "develop"], "workspace": "."},
        DeclaredTargets(paths=(".",), executables=("git",)),
    )

    assert result.status is CallStatus.COMPLETED
    assert runner.argv is not None
    assert runner.argv[0] == "docker"
    assert runner.argv[3:5] == ("--network", "none")
    assert "512m" in runner.argv
    assert runner.argv[-4:] == ("git", "push", "origin", "develop")
    assert runner.timeout == 30


@pytest.mark.commands
def test_isolated_command_timeout_is_uncertain_and_not_retried(tmp_path: Path) -> None:
    class TimeoutRunner:
        calls = 0

        def run(
            self,
            argv: tuple[str, ...],
            timeout_seconds: int,
        ) -> subprocess.CompletedProcess[str]:
            self.calls += 1
            raise subprocess.TimeoutExpired(argv, timeout_seconds)

    profile = IsolationProfileLoader().load(
        "package://mishkan.resources.isolation/local-no-network.yaml",
        tmp_path,
    )
    runner = TimeoutRunner()
    policy = policy_for(
        "command.run",
        Decision.ALLOW,
        effect_class="command",
        paths=(".",),
        executables=("python",),
    )
    context = context_for(
        tmp_path,
        "command.run",
        policy,
        (".", "python"),
        runtime="container",
        isolation_profile="local.no-network",
    )
    gateway = CapabilityGateway(
        tmp_path,
        PolicyAuthority(),
        MappingCredentialResolver({}),
        inspector(tmp_path),
        {"isolation.command": ContainerCommandAdapter(ContainerCommand(profile, runner))},
        MemoryEvidenceSink(),
    )

    result = gateway.invoke(
        context,
        {"argv": ["python", "-V"], "workspace": "."},
        DeclaredTargets(paths=(".",), executables=("python",)),
    )

    assert result.status is CallStatus.UNCERTAIN
    assert result.retryable is False
    assert runner.calls == 1


@pytest.mark.unicode
def test_unicode_confusable_target_is_refused_before_dispatch(tmp_path: Path) -> None:
    policy = policy_for(
        "repository.write_file",
        Decision.ALLOW,
        effect_class="filesystem_write",
    )
    context = context_for(tmp_path, "repository.write_file", policy, ("*",))
    adapter = RecordingAdapter(AdapterResult(output={}, actual_targets=ResolvedTargets()))
    gateway = CapabilityGateway(
        tmp_path,
        PolicyAuthority(),
        MappingCredentialResolver({}),
        inspector(tmp_path),
        {"native.repository.write_file": adapter},
        MemoryEvidenceSink(),
    )

    target = "\uff0e\uff0e/outside.txt"
    result = gateway.invoke(
        context,
        {"path": target, "content": "blocked"},
        DeclaredTargets(paths=(target,)),
    )

    assert result.status is CallStatus.REFUSED
    assert result.error_code == "ERR-TOL-003"
    assert adapter.calls == 0
