from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from support.capabilities import context_for, inspector, policy_for

from mishkan.artifacts import FilesystemArtifactStore
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.policy import Decision, PolicyAuthority
from mishkan.tools.adapters import BashShellAdapter
from mishkan.tools.crewai_gateway import GatewayCrewAITool
from mishkan.tools.gateway import CapabilityGateway, MappingCredentialResolver, MemoryEvidenceSink
from mishkan.tools.gateway_models import CallStatus, DeclaredTargets, InvocationContext


def bash_executable() -> str:
    candidate = shutil.which("bash")
    assert candidate is not None
    return str(Path(candidate).resolve())


def profile(*, startup_files: list[str] | None = None, **options: bool) -> dict[str, Any]:
    shell_options = {
        "pipefail": True,
        "errexit": False,
        "nounset": False,
        "inherit_errexit": False,
    }
    shell_options.update(options)
    return {
        "schema_version": "1.0",
        "profile_id": "bash.default",
        "revision": "1",
        "dialect": "bash",
        "interpreter": bash_executable(),
        "startup_files": startup_files or [],
        "options": shell_options,
    }


def arguments(script: str, **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "mode": "shell",
        "shell_profile": profile(),
        "script": script,
        "cwd": ".",
        "environment": {},
        "credential_environment": {},
        "stdin": None,
        "timeout_seconds": 5,
        "expected_exit_codes": [0],
        "declared_paths": [],
        "declared_executables": [],
        "network_destinations": [],
        "declared_effects": [],
        "output_policy": {
            "preview_bytes": 4096,
            "preserve_full_output_as_artifact": False,
        },
    }
    value.update(overrides)
    return value


def targets(value: dict[str, Any]) -> DeclaredTargets:
    selected_profile = value["shell_profile"]
    environment_names = tuple(
        dict.fromkeys((*value["environment"].keys(), *value["credential_environment"].keys()))
    )
    return DeclaredTargets(
        paths=(
            str(value["cwd"]),
            *selected_profile["startup_files"],
            *value["declared_paths"],
        ),
        executables=(selected_profile["interpreter"], *value["declared_executables"]),
        network_destinations=tuple(value["network_destinations"]),
        environments=environment_names,
        external_resources=tuple(value["declared_effects"]),
    )


def gateway(
    root: Path,
    *,
    credentials: MappingCredentialResolver | None = None,
    cancellation: Any = None,
    max_output_bytes: int = 64_000,
    artifact_store: Any = None,
) -> CapabilityGateway:
    adapter = BashShellAdapter(
        max_output_bytes=max_output_bytes,
        max_stdin_bytes=65_536,
        max_environment_entries=64,
        max_script_bytes=262_144,
        max_startup_file_bytes=262_144,
    )
    return CapabilityGateway(
        root,
        PolicyAuthority(),
        credentials or MappingCredentialResolver({}),
        inspector(root),
        {adapter.adapter_id: adapter},
        MemoryEvidenceSink(),
        cancellation,
        artifact_store,
    )


def shell_context(
    root: Path,
    value: dict[str, Any],
    *,
    argument_scope: tuple[str, ...] = ("*",),
    credential_scope: tuple[str, ...] = ("*",),
    allow_network: bool = True,
    memory_mb: int | None = None,
) -> InvocationContext:
    declared = targets(value)
    policy = policy_for(
        "core.shell.run",
        Decision.ALLOW,
        effect_class="command",
        paths=declared.paths or ("*",),
        executables=declared.executables or ("*",),
        arguments=argument_scope,
        network_destinations=declared.network_destinations or ("*",),
        environments=declared.environments or ("*",),
        credentials=credential_scope,
        external_resources=declared.external_resources or ("*",),
        allow_network=allow_network,
    )
    allowed = tuple(
        dict.fromkeys(
            (
                *declared.paths,
                *declared.executables,
                *declared.network_destinations,
                *declared.environments,
                *declared.external_resources,
            )
        )
    )
    return context_for(
        root,
        "core.shell.run",
        policy,
        allowed,
        network=allow_network,
        memory_mb=memory_mb,
        isolation_profile="host.explicit",
    )


@pytest.mark.commands
def test_bash_preserves_pipelines_arrays_functions_substitution_and_globbing(
    tmp_path: Path,
) -> None:
    (tmp_path / "alpha.py").write_text("", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("", encoding="utf-8")
    script = """
items=(alpha beta)
emit() { printf '%s\n' "${items[@]}"; }
joined=$(emit | while IFS= read -r item; do printf '<%s>' "$item"; done)
matches=(*.py)
printf '%s|%s' "$joined" "${matches[*]}"
"""
    value = arguments(script)
    result = gateway(tmp_path).invoke(shell_context(tmp_path, value), value, targets(value))

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert result.output["stdout_preview"] == "<alpha><beta>|alpha.py"
    assert result.output["mode"] == "shell"
    assert result.adapter_evidence["startup_mode"] == "no_profile_no_rc"
    assert result.adapter_evidence["shell_options"]["pipefail"] is True
    assert result.adapter_evidence["script_digest"].startswith("sha256:")


@pytest.mark.commands
def test_bash_sources_only_explicit_startup_and_records_declared_write_uncertain(
    tmp_path: Path,
) -> None:
    startup = tmp_path / "shell" / "project.bash"
    startup.parent.mkdir()
    startup.write_text("FROM_PROFILE=loaded\n", encoding="utf-8")
    script = """
read -r -d '' payload <<'EOF' || true
heredoc
EOF
printf '%s:%s\n' "$FROM_PROFILE" "$payload" > generated.txt
printf '%s' "$FROM_PROFILE"
"""
    value = arguments(
        script,
        shell_profile=profile(startup_files=["shell/project.bash"]),
        declared_paths=["generated.txt"],
        declared_effects=["repository.write"],
    )
    result = gateway(tmp_path).invoke(shell_context(tmp_path, value), value, targets(value))

    assert result.status is CallStatus.UNCERTAIN
    assert result.output is not None
    assert result.output["status"] == "completed"
    assert result.output["effect_settlement"] == "uncertain"
    assert (tmp_path / "generated.txt").read_text(encoding="utf-8") == "loaded:heredoc\n"
    assert result.adapter_evidence["startup_files"] == ["shell/project.bash"]


@pytest.mark.commands
def test_bash_filesystem_mutation_is_verified_with_a_diff_artifact(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / ".mishkan" / "artifacts", max_artifact_bytes=4096)
    value = arguments(
        "printf 'verified' > generated.txt",
        declared_paths=["generated.txt"],
        declared_effects=["filesystem.write"],
    )

    result = gateway(tmp_path, artifact_store=store).invoke(
        shell_context(tmp_path, value), value, targets(value)
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert result.output["changed_paths"] == ["generated.txt"]
    assert result.output["scope_deviations"] == []
    assert result.output["effect_settlement"] == "completed"
    reference = result.output["effect_diff_artifact_ref"]
    assert reference in result.external_references
    assert b'"path": "generated.txt"' in store.read_bytes(reference)


@pytest.mark.commands
def test_bash_does_not_inherit_personal_environment(tmp_path: Path) -> None:
    value = arguments('printf \'%s|%s\' "${HOME-unset}" "${BASH_ENV-unset}"')
    result = gateway(tmp_path).invoke(shell_context(tmp_path, value), value, targets(value))

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert result.output["stdout_preview"] == "unset|unset"
    assert result.adapter_evidence["ambient_environment"] is False


@pytest.mark.commands
def test_bash_profile_options_change_execution_and_are_recorded(tmp_path: Path) -> None:
    value = arguments(
        "false; printf must-not-run",
        shell_profile=profile(errexit=True, nounset=True),
    )
    result = gateway(tmp_path).invoke(shell_context(tmp_path, value), value, targets(value))

    assert result.status is CallStatus.FAILED
    assert result.output is not None
    assert result.output["exit_code"] == 1
    assert result.output["stdout_preview"] == ""
    assert result.adapter_evidence["shell_options"] == {
        "pipefail": True,
        "errexit": True,
        "nounset": True,
        "inherit_errexit": False,
    }


@pytest.mark.commands
def test_bash_reports_inherit_errexit_support_truthfully(tmp_path: Path) -> None:
    value = arguments(
        "printf supported",
        shell_profile=profile(inherit_errexit=True),
    )
    result = gateway(tmp_path).invoke(shell_context(tmp_path, value), value, targets(value))

    assert result.output is not None
    assert result.adapter_evidence["shell_options"]["inherit_errexit"] is True
    if result.output["exit_code"] == 2:
        assert result.status is CallStatus.FAILED
        assert result.output["stdout_preview"] == ""
        assert result.output["stderr_preview"] == "inherit_errexit is unsupported\n"
    else:
        assert result.status is CallStatus.COMPLETED
        assert result.output["exit_code"] == 0
        assert result.output["stdout_preview"] == "supported"


@pytest.mark.secrets
def test_bash_resolves_credentials_late_without_persisting_values(tmp_path: Path) -> None:
    secret = "bash-secret-canary-123456"
    value = arguments(
        "printf '%s' \"${TOKEN:+present}\"",
        credential_environment={"TOKEN": "service.token"},
    )
    credentials = MappingCredentialResolver({"service.token": secret})
    result = gateway(tmp_path, credentials=credentials).invoke(
        shell_context(tmp_path, value, credential_scope=("service.token",)),
        value,
        targets(value),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert result.output["stdout_preview"] == "present"
    assert credentials.calls == 1
    assert secret not in result.model_dump_json()


@pytest.mark.secrets
def test_bash_secret_literal_is_refused_before_dispatch(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    value = arguments(f"api_key=literal-secret-123456; : > {marker.name}")
    result = gateway(tmp_path).invoke(shell_context(tmp_path, value), value, targets(value))

    assert result.status is CallStatus.REFUSED
    assert result.error_code == "ERR-SEC-001"
    assert not marker.exists()


@pytest.mark.commands
def test_bash_script_policy_refuses_before_dispatch(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    value = arguments(f": > {marker.name}")
    result = gateway(tmp_path).invoke(
        shell_context(
            tmp_path,
            value,
            argument_scope=("printf-only", "bash.default", "1"),
        ),
        value,
        targets(value),
    )

    assert result.status is CallStatus.REFUSED
    assert result.error_code == "ERR-POL-001"
    assert not marker.exists()


@pytest.mark.commands
def test_bash_timeout_stops_the_shell_process_group(tmp_path: Path) -> None:
    value = arguments("while :; do :; done", timeout_seconds=1)
    result = gateway(tmp_path).invoke(shell_context(tmp_path, value), value, targets(value))

    assert result.status is CallStatus.UNCERTAIN
    assert result.retryable is False
    assert result.output is not None
    assert result.output["status"] == "timed_out"
    assert result.output["termination_cause"] == "timeout"


@pytest.mark.commands
def test_bash_unexpected_exit_is_failed(tmp_path: Path) -> None:
    value = arguments("exit 23")
    result = gateway(tmp_path).invoke(shell_context(tmp_path, value), value, targets(value))

    assert result.status is CallStatus.FAILED
    assert result.output is not None
    assert result.output["exit_code"] == 23
    assert result.output["error"] == "unexpected_exit_code"


@pytest.mark.commands
def test_native_bash_refuses_network_denial_it_cannot_enforce(tmp_path: Path) -> None:
    value = arguments("printf must-not-run")
    result = gateway(tmp_path).invoke(
        shell_context(tmp_path, value, allow_network=False),
        value,
        targets(value),
    )

    assert result.status is CallStatus.REFUSED
    assert result.error_code == "ERR-TOL-002"


@pytest.mark.commands
def test_native_bash_refuses_an_unenforceable_strict_memory_limit(tmp_path: Path) -> None:
    value = arguments("printf must-not-run")
    result = gateway(tmp_path).invoke(
        shell_context(tmp_path, value, memory_mb=512), value, targets(value)
    )

    assert result.status is CallStatus.REFUSED
    assert result.error_code == "ERR-TOL-002"


@pytest.mark.commands
def test_bash_is_a_current_crewai_tool_binding(tmp_path: Path) -> None:
    value = arguments("printf through-crewai")
    context = shell_context(tmp_path, value)
    contract = context.registry.require("core.shell.run")
    tool = GatewayCrewAITool(contract, gateway(tmp_path), context)

    output = json.loads(tool.run(**value))

    assert output["stdout_preview"] == "through-crewai"
    assert output["mode"] == "shell"


@pytest.mark.commands
def test_bash_large_output_uses_the_same_immutable_artifact_surface(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / ".mishkan" / "artifacts", max_artifact_bytes=4096)
    value = arguments(
        "printf '%01000d' 0",
        output_policy={"preview_bytes": 32, "preserve_full_output_as_artifact": True},
    )
    result = gateway(tmp_path, artifact_store=store).invoke(
        shell_context(tmp_path, value), value, targets(value)
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    reference = result.output["stdout_artifact_ref"]
    assert len(store.read_bytes(reference)) == 1000
    assert result.output["stdout_preview"] == "0" * 32


@pytest.mark.commands
def test_artifact_failure_preserves_an_already_uncertain_shell_effect(tmp_path: Path) -> None:
    class FailingStore:
        def put_bytes(self, *_: Any, **__: Any) -> Any:
            raise MishkanError(ErrorCode.ARTIFACT, "injected artifact failure")

    value = arguments(
        ": > changed.txt; printf 'large-output'",
        declared_paths=["changed.txt"],
        declared_effects=["repository.write"],
        output_policy={"preview_bytes": 1, "preserve_full_output_as_artifact": True},
    )
    result = gateway(tmp_path, artifact_store=FailingStore()).invoke(
        shell_context(tmp_path, value), value, targets(value)
    )

    assert result.status is CallStatus.UNCERTAIN
    assert result.error_code == "ERR-ART-001"
    assert (tmp_path / "changed.txt").exists()
