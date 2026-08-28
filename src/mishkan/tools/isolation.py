"""Visible Docker/Podman isolation profiles and argument-vector command construction."""

from __future__ import annotations

import os
import selectors
import shutil
import signal
import subprocess
import time
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.schema import SchemaRegistry
from mishkan.domain.sources import resolve_source_path


class IsolationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContainerRuntime(StrEnum):
    DOCKER = "docker"
    PODMAN = "podman"


class WorkspaceMount(StrEnum):
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


class IsolationProfile(IsolationModel):
    schema_version: str = "1.0"
    profile_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    adoption_authority: str = Field(min_length=1)
    runtime: ContainerRuntime
    image: str = Field(min_length=1)
    network_mode: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1, le=86_400)
    memory_mb: int = Field(ge=1)
    pids_limit: int = Field(ge=1)
    read_only_root: bool
    workspace_mount: WorkspaceMount
    workspace_target: str = Field(pattern=r"^/[^\x00]*$")
    environment_allowlist: tuple[str, ...]


class ProcessRunner(Protocol):
    def run(
        self, argv: tuple[str, ...], timeout_seconds: int
    ) -> subprocess.CompletedProcess[str]: ...


class ContainerOutputLimit(subprocess.SubprocessError):
    def __init__(self, stdout: bytes, stderr: bytes) -> None:
        super().__init__("isolated command exceeded its configured output limit")
        self.stdout = stdout
        self.stderr = stderr


class SubprocessRunner:
    def __init__(self, *, max_output_bytes: int = 2_097_152) -> None:
        if max_output_bytes < 1:
            raise ValueError("isolated command output bound must be positive")
        self._max_output_bytes = max_output_bytes

    def run(self, argv: tuple[str, ...], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        selector = selectors.DefaultSelector()
        streams = {process.stdout: "stdout", process.stderr: "stderr"}
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = time.monotonic() + timeout_seconds
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate(process)
                    raise subprocess.TimeoutExpired(
                        argv,
                        timeout_seconds,
                        output=bytes(buffers["stdout"]),
                        stderr=bytes(buffers["stderr"]),
                    )
                for key, _ in selector.select(min(remaining, 0.1)):
                    selected = cast(BinaryIO, key.fileobj)
                    chunk = os.read(selected.fileno(), 65_536)
                    if not chunk:
                        selector.unregister(selected)
                        selected.close()
                        continue
                    buffers[streams[selected]].extend(chunk)
                    if sum(len(value) for value in buffers.values()) > self._max_output_bytes:
                        self._terminate(process)
                        raise ContainerOutputLimit(
                            bytes(buffers["stdout"]), bytes(buffers["stderr"])
                        )
            return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            return subprocess.CompletedProcess(
                argv,
                return_code,
                bytes(buffers["stdout"]).decode("utf-8", errors="replace"),
                bytes(buffers["stderr"]).decode("utf-8", errors="replace"),
            )
        finally:
            selector.close()
            if process.poll() is None:
                self._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=0.5)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            process.wait(timeout=1)


class ContainerCommand:
    def __init__(
        self,
        profile: IsolationProfile,
        runner: ProcessRunner,
        *,
        image_identity: str | None = None,
    ) -> None:
        self.profile = profile
        self._runner = runner
        self.image_identity = image_identity or profile.image

    def build(
        self,
        workspace: Path,
        command: tuple[str, ...],
        *,
        environment_names: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        if not command:
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "container command argv is empty")
        try:
            resolved_workspace = workspace.resolve(strict=True)
        except OSError as exc:
            raise MishkanError(
                ErrorCode.FILE,
                "isolated command workspace is unavailable",
            ) from exc
        if not resolved_workspace.is_dir():
            raise MishkanError(
                ErrorCode.FILE,
                "isolated command workspace must be a directory",
            )
        mount = f"type=bind,src={resolved_workspace},dst={self.profile.workspace_target}"
        if self.profile.workspace_mount is WorkspaceMount.READ_ONLY:
            mount = f"{mount},readonly"
        argv = [
            self.profile.runtime.value,
            "run",
            "--rm",
            "--network",
            self.profile.network_mode,
            "--memory",
            f"{self.profile.memory_mb}m",
            "--pids-limit",
            str(self.profile.pids_limit),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            mount,
            "--workdir",
            self.profile.workspace_target,
        ]
        if self.profile.read_only_root:
            argv.append("--read-only")
        allowed_environment = set(self.profile.environment_allowlist)
        if not set(environment_names).issubset(allowed_environment):
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "isolated command environment exceeds its public profile",
            )
        for name in environment_names:
            argv.extend(("--env", name))
        argv.extend((self.image_identity, *command))
        return tuple(argv)

    def run(self, workspace: Path, command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return self._runner.run(self.build(workspace, command), self.profile.timeout_seconds)


def observe_container_commands(
    profiles: tuple[IsolationProfile, ...],
    *,
    runner: ProcessRunner | None = None,
) -> dict[str, ContainerCommand]:
    """Return only profiles whose runtime and configured image are ready without pulling."""

    execution_runner = runner or SubprocessRunner()
    commands: dict[str, ContainerCommand] = {}
    for profile in profiles:
        executable = shutil.which(profile.runtime.value)
        if executable is None:
            continue
        inspect_argv = (
            executable,
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            profile.image,
        )
        try:
            completed = execution_runner.run(inspect_argv, 5)
        except (OSError, subprocess.SubprocessError):
            continue
        image_identity = completed.stdout.strip()
        if completed.returncode != 0 or not image_identity:
            continue
        commands[profile.profile_id] = ContainerCommand(
            profile,
            execution_runner,
            image_identity=image_identity,
        )
    return commands


class IsolationProfileLoader:
    def load(self, uri: str, project_root: Path) -> IsolationProfile:
        raw = self._read(uri, project_root)
        try:
            document: Any = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "isolation profile is malformed YAML",
                details={"source": uri},
            ) from exc
        if not isinstance(document, dict):
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "isolation profile must contain a mapping",
                details={"source": uri},
            )
        SchemaRegistry.require_supported("mishkan.isolation", document.get("schema_version"))
        try:
            return IsolationProfile.model_validate(document)
        except PydanticValidationError as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "isolation profile is invalid",
                details={"source": uri, "violations": len(exc.errors())},
            ) from exc

    @staticmethod
    def _read(uri: str, project_root: Path) -> bytes:
        if uri.startswith("package://"):
            location = uri.removeprefix("package://")
            module, separator, resource = location.partition("/")
            if not separator:
                raise MishkanError(
                    ErrorCode.CONFIGURATION,
                    "package isolation URI must identify a module and resource",
                    details={"source": uri},
                )
            return files(module).joinpath(resource).read_bytes()
        path = resolve_source_path(uri, project_root, "isolation profile")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "isolation profile cannot be read",
                details={"source": uri, "reason": type(exc).__name__},
            ) from exc
