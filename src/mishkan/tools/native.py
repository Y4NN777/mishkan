"""Truthful discovery and construction of bundled native capability adapters."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.tools.adapters import (
    BashShellAdapter,
    CapabilityAdapter,
    DirectProcessAdapter,
    FileListAdapter,
    FileReadAdapter,
    FileResolveAdapter,
    FileStatAdapter,
    GitHistorySearchAdapter,
    PythonStructuralSearchAdapter,
    PythonSymbolSearchAdapter,
    ReadFileAdapter,
    RipgrepTextSearchAdapter,
    SearchFilesAdapter,
)
from mishkan.tools.catalog import ToolCatalog
from mishkan.tools.models import ToolContract


@dataclass(frozen=True, slots=True)
class ExecutableObservation:
    name: str
    path: str


@dataclass(frozen=True, slots=True)
class NativeCapabilityEnvironment:
    dependencies: frozenset[str]
    adapter_ids: frozenset[str]
    executables: tuple[ExecutableObservation, ...]


def discover_native_environment(
    path_value: str | None = None,
    *,
    max_executables: int = 128,
    required_executables: tuple[str, ...] = ("git",),
) -> NativeCapabilityEnvironment:
    """Observe executable availability without executing repository-controlled content."""

    if max_executables < 1:
        raise ValueError("executable observation bound must be positive")
    search_path = path_value if path_value is not None else os.environ.get("PATH", "")
    observations: dict[str, str] = {}
    for raw_directory in search_path.split(os.pathsep):
        if not raw_directory:
            continue
        directory = Path(raw_directory).expanduser()
        try:
            entries = sorted(directory.iterdir(), key=lambda item: os.fsencode(item.name))
        except OSError:
            continue
        for entry in entries:
            if entry.name in observations or not entry.is_file() or not os.access(entry, os.X_OK):
                continue
            try:
                canonical = entry.resolve(strict=True)
            except OSError:
                continue
            observations[entry.name] = str(canonical)
            if len(observations) >= max_executables:
                break
        if len(observations) >= max_executables:
            break

    dependencies = frozenset(
        dependency
        for dependency in ("bash", "git", "rg")
        if shutil.which(dependency, path=search_path)
    )
    for required_name in (*required_executables, *sorted(dependencies)):
        observed_path = shutil.which(required_name, path=search_path)
        if observed_path is not None:
            observations[required_name] = str(Path(observed_path).resolve(strict=True))
    adapter_ids = {
        FileResolveAdapter.adapter_id,
        FileStatAdapter.adapter_id,
        FileReadAdapter.adapter_id,
        FileListAdapter.adapter_id,
        SearchFilesAdapter.adapter_id,
        PythonStructuralSearchAdapter.adapter_id,
        PythonSymbolSearchAdapter.adapter_id,
        ReadFileAdapter.adapter_id,
        DirectProcessAdapter.adapter_id,
    }
    if "rg" in dependencies:
        adapter_ids.add(RipgrepTextSearchAdapter.adapter_id)
    if "git" in dependencies:
        adapter_ids.add(GitHistorySearchAdapter.adapter_id)
    if "bash" in dependencies:
        adapter_ids.add(BashShellAdapter.adapter_id)
    return NativeCapabilityEnvironment(
        dependencies=dependencies,
        adapter_ids=frozenset(adapter_ids),
        executables=tuple(
            ExecutableObservation(name=name, path=path)
            for name, path in sorted(observations.items())
        ),
    )


def build_native_adapters(
    catalog: ToolCatalog,
    tool_ids: tuple[str, ...],
    environment: NativeCapabilityEnvironment,
) -> Mapping[str, CapabilityAdapter]:
    """Construct only adapters whose contracts are bindable in this environment."""

    contracts: dict[str, ToolContract] = {}
    for tool_id in tool_ids:
        try:
            contract = catalog.snapshot((tool_id,)).require(tool_id)
        except MishkanError as error:
            if error.envelope.code is ErrorCode.TOOL_UNAVAILABLE:
                continue
            raise
        contracts[contract.adapter] = contract

    adapters: dict[str, CapabilityAdapter] = {}
    for adapter_id, contract in contracts.items():
        config = contract.adapter_config
        if adapter_id == FileResolveAdapter.adapter_id:
            adapters[adapter_id] = FileResolveAdapter()
        elif adapter_id == FileStatAdapter.adapter_id:
            adapters[adapter_id] = FileStatAdapter(int(config["max_digest_bytes"]))
        elif adapter_id == FileReadAdapter.adapter_id:
            adapters[adapter_id] = FileReadAdapter(
                int(config["max_bytes"]), int(config["max_scan_bytes"])
            )
        elif adapter_id == FileListAdapter.adapter_id:
            adapters[adapter_id] = FileListAdapter(
                int(config["max_results"]), int(config["max_traversal_entries"])
            )
        elif adapter_id == SearchFilesAdapter.adapter_id:
            adapters[adapter_id] = SearchFilesAdapter(
                int(config["max_results"]), int(config["max_traversal_entries"])
            )
        elif adapter_id == RipgrepTextSearchAdapter.adapter_id:
            executable = _observed_executable(environment, "rg")
            adapters[adapter_id] = RipgrepTextSearchAdapter(
                executable,
                _first_version_line(executable),
                max_results=int(config["max_results"]),
                max_output_bytes=int(config["max_output_bytes"]),
                timeout_seconds=contract.resources.timeout_seconds,
            )
        elif adapter_id == PythonStructuralSearchAdapter.adapter_id:
            adapters[adapter_id] = PythonStructuralSearchAdapter(
                max_results=int(config["max_results"]),
                max_files=int(config["max_files"]),
                max_file_bytes=int(config["max_file_bytes"]),
            )
        elif adapter_id == PythonSymbolSearchAdapter.adapter_id:
            adapters[adapter_id] = PythonSymbolSearchAdapter(
                max_results=int(config["max_results"]),
                max_files=int(config["max_files"]),
                max_file_bytes=int(config["max_file_bytes"]),
            )
        elif adapter_id == GitHistorySearchAdapter.adapter_id:
            executable = _observed_executable(environment, "git")
            adapters[adapter_id] = GitHistorySearchAdapter(
                executable,
                _first_version_line(executable),
                max_results=int(config["max_results"]),
                timeout_seconds=contract.resources.timeout_seconds,
            )
        elif adapter_id == ReadFileAdapter.adapter_id:
            adapters[adapter_id] = ReadFileAdapter(contract.max_bytes)
        elif adapter_id == DirectProcessAdapter.adapter_id:
            adapters[adapter_id] = DirectProcessAdapter(
                max_output_bytes=int(config["max_output_bytes"]),
                max_stdin_bytes=int(config["max_stdin_bytes"]),
                max_environment_entries=int(config["max_environment_entries"]),
            )
        elif adapter_id == BashShellAdapter.adapter_id:
            adapters[adapter_id] = BashShellAdapter(
                max_output_bytes=int(config["max_output_bytes"]),
                max_stdin_bytes=int(config["max_stdin_bytes"]),
                max_environment_entries=int(config["max_environment_entries"]),
                max_script_bytes=int(config["max_script_bytes"]),
                max_startup_file_bytes=int(config["max_startup_file_bytes"]),
            )
        elif adapter_id == "isolation.command":
            continue
        else:
            raise MishkanError(
                ErrorCode.TOOL_UNAVAILABLE,
                "bundled native adapter has no construction path",
                details={"adapter": adapter_id},
            )
    return adapters


def available_contracts(
    catalog: ToolCatalog,
    tool_ids: tuple[str, ...],
) -> tuple[ToolContract, ...]:
    contracts: list[ToolContract] = []
    for tool_id in tool_ids:
        try:
            contracts.append(catalog.snapshot((tool_id,)).require(tool_id))
        except MishkanError as error:
            if error.envelope.code is not ErrorCode.TOOL_UNAVAILABLE:
                raise
    return tuple(contracts)


def _observed_executable(
    environment: NativeCapabilityEnvironment,
    name: str,
) -> Path:
    matches = [item.path for item in environment.executables if item.name == name]
    if len(matches) != 1:
        raise MishkanError(
            ErrorCode.TOOL_UNAVAILABLE,
            "required native executable was not observed exactly once",
            details={"executable": name},
        )
    return Path(matches[0])


def _first_version_line(executable: Path) -> str:
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            env={"LC_ALL": "C.UTF-8"},
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise MishkanError(
            ErrorCode.TOOL_UNAVAILABLE,
            "native executable version cannot be observed",
            details={"executable": str(executable)},
        ) from exc
    return completed.stdout.splitlines()[0].strip()
