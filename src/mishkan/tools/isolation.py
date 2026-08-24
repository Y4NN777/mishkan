"""Visible Docker/Podman isolation profiles and argument-vector command construction."""

from __future__ import annotations

import subprocess
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.schema import SchemaRegistry


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


class SubprocessRunner:
    def run(self, argv: tuple[str, ...], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )


class ContainerCommand:
    def __init__(self, profile: IsolationProfile, runner: ProcessRunner) -> None:
        self.profile = profile
        self._runner = runner

    def build(self, workspace: Path, command: tuple[str, ...]) -> tuple[str, ...]:
        if not command:
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "container command argv is empty")
        mount = f"type=bind,src={workspace.resolve()},dst={self.profile.workspace_target}"
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
            "--mount",
            mount,
            "--workdir",
            self.profile.workspace_target,
        ]
        if self.profile.read_only_root:
            argv.append("--read-only")
        argv.extend((self.profile.image, *command))
        return tuple(argv)

    def run(self, workspace: Path, command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return self._runner.run(self.build(workspace, command), self.profile.timeout_seconds)


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
        path = (
            project_root / uri.removeprefix("project:") if uri.startswith("project:") else Path(uri)
        )
        if not path.is_absolute():
            path = project_root / path
        try:
            return path.resolve().read_bytes()
        except OSError as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "isolation profile cannot be read",
                details={"source": uri, "reason": type(exc).__name__},
            ) from exc
