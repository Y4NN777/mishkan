from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from support.i02 import context_for, inspector, policy_for

from mishkan.artifacts import ArtifactLifecycle, ArtifactValidation, FilesystemArtifactStore
from mishkan.policy import Decision, PolicyAuthority
from mishkan.tools.adapters import DirectProcessAdapter
from mishkan.tools.crewai_gateway import GatewayCrewAITool
from mishkan.tools.gateway import CapabilityGateway, MappingCredentialResolver, MemoryEvidenceSink
from mishkan.tools.gateway_models import CallStatus, DeclaredTargets, InvocationContext


def executable() -> str:
    return str(Path(sys.executable).resolve())


def arguments(*args: str, **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "mode": "process",
        "executable": executable(),
        "args": list(args),
        "cwd": ".",
        "environment": {},
        "credential_environment": {},
        "stdin": None,
        "timeout_seconds": 5,
        "expected_exit_codes": [0],
        "declared_effects": [],
        "output_policy": {
            "preview_bytes": 4096,
            "preserve_full_output_as_artifact": False,
        },
    }
    value.update(overrides)
    return value


def targets(value: dict[str, Any]) -> DeclaredTargets:
    environment = tuple(
        dict.fromkeys((*value["environment"].keys(), *value["credential_environment"].keys()))
    )
    return DeclaredTargets(
        paths=(str(value["cwd"]),),
        executables=(str(value["executable"]),),
        environments=environment,
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
    adapter = DirectProcessAdapter(
        max_output_bytes=max_output_bytes,
        max_stdin_bytes=65_536,
        max_environment_entries=64,
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


def process_context(
    root: Path,
    value: dict[str, Any],
    *,
    credential_scope: tuple[str, ...] = ("*",),
    argument_scope: tuple[str, ...] = ("*",),
    decision: Decision = Decision.ALLOW,
    memory_mb: int | None = None,
) -> InvocationContext:
    environment_names = tuple(
        dict.fromkeys((*value["environment"].keys(), *value["credential_environment"].keys()))
    )
    policy = policy_for(
        "core.process.exec",
        decision,
        effect_class="command",
        paths=(str(value["cwd"]),),
        executables=(str(value["executable"]),),
        arguments=argument_scope,
        environments=environment_names or ("*",),
        credentials=credential_scope,
        external_resources=tuple(value["declared_effects"]) or ("*",),
        allow_network=True,
    )
    allowed = tuple(
        dict.fromkeys(
            (
                str(value["cwd"]),
                str(value["executable"]),
                *environment_names,
                *value["declared_effects"],
            )
        )
    )
    return context_for(
        root,
        "core.process.exec",
        policy,
        allowed,
        network=True,
        memory_mb=memory_mb,
    )


@pytest.mark.commands
def test_native_process_refuses_an_unenforceable_strict_memory_limit(
    tmp_path: Path,
) -> None:
    value = arguments("-c", "print('must not run')")

    result = gateway(tmp_path).invoke(
        process_context(tmp_path, value, memory_mb=512), value, targets(value)
    )

    assert result.status is CallStatus.REFUSED
    assert result.error_code == "ERR-TOL-002"


@pytest.mark.commands
def test_process_passes_shell_tokens_as_literal_argv_without_ambient_environment(
    tmp_path: Path,
) -> None:
    script = "import json,os,sys;print(json.dumps([sys.argv[1:],os.environ.get('HOME')]))"
    literal = ("*", "$HOME", "a;b", "x|y", ">output")
    value = arguments("-c", script, *literal)
    result = gateway(tmp_path).invoke(process_context(tmp_path, value), value, targets(value))

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    observed = json.loads(result.output["stdout_preview"])
    assert observed == [list(literal), None]
    assert result.output["status"] == "completed"
    assert result.output["effect_settlement"] == "absent"
    assert result.adapter_evidence["shell"] is False
    assert result.actual_targets is not None
    assert result.actual_targets.executables == (executable(),)


@pytest.mark.commands
def test_process_uses_explicit_cwd_environment_stdin_and_expected_exit_codes(
    tmp_path: Path,
) -> None:
    (tmp_path / "project").mkdir()
    script = (
        "import json,os,pathlib,sys;"
        "print(json.dumps([pathlib.Path.cwd().name,os.environ['MODE'],sys.stdin.read()]));"
        "raise SystemExit(7)"
    )
    value = arguments(
        "-c",
        script,
        cwd="project",
        environment={"MODE": "test"},
        stdin="payload",
        expected_exit_codes=[7],
    )
    result = gateway(tmp_path).invoke(process_context(tmp_path, value), value, targets(value))

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert json.loads(result.output["stdout_preview"]) == ["project", "test", "payload"]
    assert result.output["exit_code"] == 7
    assert result.output["status"] == "completed"
    assert result.output["environment_names"] == ["MODE"]


@pytest.mark.commands
def test_unexpected_exit_code_is_a_failed_tool_call(tmp_path: Path) -> None:
    value = arguments("-c", "raise SystemExit(9)")
    result = gateway(tmp_path).invoke(process_context(tmp_path, value), value, targets(value))

    assert result.status is CallStatus.FAILED
    assert result.retryable is False
    assert result.error_code == "ERR-EXE-001"
    assert result.output is not None
    assert result.output["status"] == "failed"
    assert result.output["exit_code"] == 9
    assert result.output["error"] == "unexpected_exit_code"


@pytest.mark.secrets
def test_process_resolves_credential_environment_after_authorization_without_persisting_value(
    tmp_path: Path,
) -> None:
    secret = "process-secret-canary-123456"
    script = "import os;print('present' if os.environ.get('TOKEN') else 'missing')"
    value = arguments(
        "-c",
        script,
        credential_environment={"TOKEN": "service.token"},
    )
    credentials = MappingCredentialResolver({"service.token": secret})
    result = gateway(tmp_path, credentials=credentials).invoke(
        process_context(tmp_path, value, credential_scope=("service.token",)),
        value,
        targets(value),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert result.output["stdout_preview"].strip() == "present"
    assert result.output["credential_environment_names"] == ["TOKEN"]
    assert credentials.calls == 1
    assert secret not in result.model_dump_json()


@pytest.mark.secrets
def test_process_secret_output_is_contained_by_full_output_inspection(tmp_path: Path) -> None:
    secret = "process-output-secret-canary-987654"
    script = "import os;print(os.environ['TOKEN'])"
    value = arguments(
        "-c",
        script,
        credential_environment={"TOKEN": "service.token"},
    )
    credentials = MappingCredentialResolver({"service.token": secret})
    result = gateway(tmp_path, credentials=credentials).invoke(
        process_context(tmp_path, value, credential_scope=("service.token",)),
        value,
        targets(value),
    )

    assert result.status is CallStatus.FAILED
    assert result.error_code == "ERR-SEC-001"
    assert result.output is None
    assert secret not in result.model_dump_json()


@pytest.mark.secrets
def test_process_credential_scope_is_denied_before_resolution(tmp_path: Path) -> None:
    value = arguments(
        "-c",
        "print('must not run')",
        credential_environment={"TOKEN": "service.token"},
    )
    credentials = MappingCredentialResolver({"service.token": "unresolved-canary"})
    result = gateway(tmp_path, credentials=credentials).invoke(
        process_context(tmp_path, value, credential_scope=("different.token",)),
        value,
        targets(value),
    )

    assert result.status is CallStatus.REFUSED
    assert result.error_code == "ERR-POL-001"
    assert credentials.calls == 0


@pytest.mark.commands
def test_process_argument_policy_refuses_before_dispatch(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    value = arguments("-c", f"from pathlib import Path;Path({str(marker)!r}).touch()")
    result = gateway(tmp_path).invoke(
        process_context(tmp_path, value, argument_scope=("-c", "print-only")),
        value,
        targets(value),
    )

    assert result.status is CallStatus.REFUSED
    assert result.error_code == "ERR-POL-001"
    assert not marker.exists()


@pytest.mark.commands
def test_process_timeout_stops_the_process_group_and_settles_uncertain(tmp_path: Path) -> None:
    value = arguments("-c", "import time;time.sleep(5)", timeout_seconds=1)
    result = gateway(tmp_path).invoke(process_context(tmp_path, value), value, targets(value))

    assert result.status is CallStatus.UNCERTAIN
    assert result.retryable is False
    assert result.output is not None
    assert result.output["status"] == "timed_out"
    assert result.output["termination_cause"] == "timeout"


@pytest.mark.commands
def test_process_cancellation_is_observed_during_execution(tmp_path: Path) -> None:
    class Cancellation:
        def __init__(self) -> None:
            self.calls = 0

        def requested(self, run_id: str, task_attempt_id: str) -> bool:
            assert (run_id, task_attempt_id) == ("run-1", "task:1")
            self.calls += 1
            return self.calls >= 3

    cancellation = Cancellation()
    value = arguments("-c", "import time;time.sleep(5)")
    result = gateway(tmp_path, cancellation=cancellation).invoke(
        process_context(tmp_path, value), value, targets(value)
    )

    assert result.status is CallStatus.CANCELLED
    assert result.retryable is False
    assert result.output is not None
    assert result.output["status"] == "cancelled"
    assert cancellation.calls >= 3


@pytest.mark.commands
def test_native_process_refuses_network_denial_it_cannot_enforce(tmp_path: Path) -> None:
    value = arguments("-c", "print('must not run')")
    policy = policy_for(
        "core.process.exec",
        Decision.ALLOW,
        effect_class="command",
        paths=(".",),
        executables=(executable(),),
        allow_network=False,
    )
    context = context_for(
        tmp_path,
        "core.process.exec",
        policy,
        (".", executable()),
        network=False,
    )
    result = gateway(tmp_path).invoke(context, value, targets(value))

    assert result.status is CallStatus.REFUSED
    assert result.error_code == "ERR-TOL-002"


@pytest.mark.commands
def test_process_output_limit_terminates_without_unbounded_result(tmp_path: Path) -> None:
    value = arguments(
        "-c",
        "print('x' * 100000)",
        output_policy={"preview_bytes": 128, "preserve_full_output_as_artifact": False},
    )
    result = gateway(tmp_path, max_output_bytes=512).invoke(
        process_context(tmp_path, value), value, targets(value)
    )

    assert result.status is CallStatus.UNCERTAIN
    assert result.output is not None
    assert result.output["termination_cause"] == "output_limit"
    assert result.output["truncated"] is True
    assert len(result.output["stdout_preview"].encode()) <= 128


@pytest.mark.commands
def test_process_large_output_is_committed_as_an_immutable_artifact(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / ".mishkan" / "artifacts", max_artifact_bytes=4096)
    value = arguments(
        "-c",
        "print('artifact-output-' * 100)",
        output_policy={"preview_bytes": 64, "preserve_full_output_as_artifact": True},
    )
    result = gateway(tmp_path, artifact_store=store).invoke(
        process_context(tmp_path, value), value, targets(value)
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    reference = result.output["stdout_artifact_ref"]
    assert reference in result.external_references
    assert store.read_bytes(reference) == ("artifact-output-" * 100 + "\n").encode()
    manifest = store.read_manifest(reference)
    assert manifest.lifecycle is ArtifactLifecycle.AVAILABLE
    assert manifest.validation is ArtifactValidation.INTEGRITY_VERIFIED
    assert manifest.acceptance == "unaccepted"
    assert manifest.declared_media_type == "text/plain; charset=utf-8"
    assert manifest.detected_media_type is None
    assert manifest.provenance.call_id == result.call_id
    assert manifest.provenance.channel == "stdout"


@pytest.mark.commands
def test_output_limit_artifact_is_partial_and_quarantined(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / ".mishkan" / "artifacts", max_artifact_bytes=1024)
    value = arguments(
        "-c",
        "print('x' * 100000)",
        output_policy={"preview_bytes": 64, "preserve_full_output_as_artifact": True},
    )
    result = gateway(tmp_path, max_output_bytes=512, artifact_store=store).invoke(
        process_context(tmp_path, value), value, targets(value)
    )

    assert result.status is CallStatus.UNCERTAIN
    assert result.output is not None
    reference = result.output["stdout_artifact_ref"]
    manifest = store.read_manifest(reference)
    assert manifest.lifecycle is ArtifactLifecycle.QUARANTINED
    assert manifest.validation is ArtifactValidation.PARTIAL
    assert len(store.read_bytes(reference)) == 512


@pytest.mark.commands
def test_process_artifact_request_is_refused_before_dispatch_without_store(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    value = arguments(
        "-c",
        f"from pathlib import Path;Path({str(marker)!r}).touch()",
        output_policy={"preview_bytes": 64, "preserve_full_output_as_artifact": True},
    )
    result = gateway(tmp_path).invoke(process_context(tmp_path, value), value, targets(value))

    assert result.status is CallStatus.REFUSED
    assert result.error_code == "ERR-TOL-002"
    assert not marker.exists()


@pytest.mark.secrets
def test_secret_output_never_reaches_artifact_storage(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / ".mishkan" / "artifacts", max_artifact_bytes=4096)
    secret = "artifact-secret-canary-123456"
    value = arguments(
        "-c",
        "import os;print(os.environ['TOKEN'] * 4)",
        credential_environment={"TOKEN": "service.token"},
        output_policy={"preview_bytes": 8, "preserve_full_output_as_artifact": True},
    )
    result = gateway(
        tmp_path,
        credentials=MappingCredentialResolver({"service.token": secret}),
        artifact_store=store,
    ).invoke(
        process_context(tmp_path, value, credential_scope=("service.token",)),
        value,
        targets(value),
    )

    assert result.status is CallStatus.FAILED
    assert result.error_code == "ERR-SEC-001"
    assert not tuple((tmp_path / ".mishkan" / "artifacts" / "manifests").iterdir())


@pytest.mark.commands
def test_unverified_declared_effect_remains_uncertain_after_zero_exit(tmp_path: Path) -> None:
    value = arguments(
        "-c",
        "print('process-exited')",
        declared_effects=["repository.write"],
    )
    result = gateway(tmp_path).invoke(process_context(tmp_path, value), value, targets(value))

    assert result.status is CallStatus.UNCERTAIN
    assert result.retryable is False
    assert result.output is not None
    assert result.output["status"] == "completed"
    assert result.output["effect_settlement"] == "uncertain"
    assert result.output["declared_effects"] == ["repository.write"]


@pytest.mark.commands
def test_process_is_a_current_crewai_tool_binding(tmp_path: Path) -> None:
    value = arguments("-c", "print('through-crewai')")
    context = process_context(tmp_path, value)
    contract = context.registry.require("core.process.exec")
    tool = GatewayCrewAITool(contract, gateway(tmp_path), context)

    output = json.loads(tool.run(**value))

    assert output["stdout_preview"].strip() == "through-crewai"
    assert output["mode"] == "process"
