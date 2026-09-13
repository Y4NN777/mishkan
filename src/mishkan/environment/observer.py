"""Bounded non-mutating project and machine environment observation."""

from __future__ import annotations

import hashlib
import os
import platform as host_platform
import shutil
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.environment.models import (
    AvailabilityDimension,
    AvailabilityFact,
    AvailabilityState,
    DescriptorObservation,
    EngineObservation,
    EnvironmentObservation,
    EnvironmentObservationRequest,
)
from mishkan.environment.profile import EngineProbeDefinition, EnvironmentProfile

_DIMENSIONS: tuple[AvailabilityDimension, ...] = (
    "inventoried",
    "detected",
    "installed",
    "executable",
    "authenticated",
    "healthy",
    "project_used",
    "eligible",
    "authorized",
)


class EnvironmentObserver:
    def __init__(self, profile: EnvironmentProfile) -> None:
        self._profile = profile

    def observe(
        self,
        workspace: Path,
        *,
        request: EnvironmentObservationRequest,
        path_value: str | None = None,
    ) -> EnvironmentObservation:
        root = workspace.resolve(strict=True)
        descriptors = self._descriptors(root, request.repository_revision or "unversioned")
        manifests = self._manifests(root)
        marker_paths = {item.logical_path for item in descriptors} | {
            path for values in manifests.values() for path in values
        }
        search_path = os.environ.get("PATH", "") if path_value is None else path_value
        platform_name = host_platform.system().casefold()
        architecture = host_platform.machine().casefold()
        engines = tuple(
            self._engine(engine, search_path, marker_paths, platform_name)
            for engine in self._profile.engines
        )
        unknowns = tuple(
            f"engine:{item.engine_id}:version"
            for item in engines
            if item.version is None and item.fact("detected") is AvailabilityState.TRUE
        )
        return EnvironmentObservation(
            observation_id=request.observation_id,
            context_id=request.context_id,
            repository_id=request.repository_id,
            repository_revision=request.repository_revision,
            workspace=root,
            execution_location=request.execution_location,
            platform=platform_name,
            architecture=architecture,
            profile_id=self._profile.profile_id,
            profile_revision=self._profile.revision,
            descriptors=descriptors,
            manifests=manifests,
            engines=engines,
            unknowns=unknowns,
        )

    def _descriptors(
        self,
        root: Path,
        repository_revision: str,
    ) -> tuple[DescriptorObservation, ...]:
        observed: list[DescriptorObservation] = []
        for format_name, patterns in sorted(self._profile.descriptor_paths.items()):
            for pattern in patterns:
                self._validate_pattern(pattern)
                for path in sorted(root.glob(pattern), key=lambda item: item.as_posix()):
                    if len(observed) >= self._profile.max_observed_files:
                        raise MishkanError(
                            ErrorCode.OUTPUT_CONTRACT,
                            "environment descriptor observation exceeds its configured bound",
                        )
                    content = self._safe_read(root, path, self._profile.max_descriptor_bytes)
                    observed.append(
                        DescriptorObservation(
                            format=format_name,
                            logical_path=path.relative_to(root).as_posix(),
                            digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
                            size_bytes=len(content),
                            repository_revision=repository_revision,
                        )
                    )
        unique = {item.logical_path: item for item in observed}
        return tuple(unique[path] for path in sorted(unique))

    def _manifests(self, root: Path) -> dict[str, tuple[str, ...]]:
        names = {
            name: ecosystem
            for ecosystem, values in self._profile.manifest_names.items()
            for name in values
        }
        found: dict[str, list[str]] = {}
        visited = 0
        excluded = set(self._profile.excluded_directories)
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            relative_directory = current_path.relative_to(root)
            if len(relative_directory.parts) >= self._profile.max_manifest_depth:
                directories[:] = []
            else:
                directories[:] = sorted(
                    name
                    for name in directories
                    if name not in excluded and not (current_path / name).is_symlink()
                )
            for filename in sorted(filenames):
                visited += 1
                if visited > self._profile.max_observed_files:
                    raise MishkanError(
                        ErrorCode.OUTPUT_CONTRACT,
                        "environment manifest observation exceeds its configured bound",
                    )
                ecosystem = names.get(filename)
                if ecosystem is None:
                    continue
                path = current_path / filename
                if path.is_symlink() or not path.is_file():
                    continue
                found.setdefault(ecosystem, []).append(path.relative_to(root).as_posix())
        return {key: tuple(values) for key, values in sorted(found.items())}

    def _engine(
        self,
        definition: EngineProbeDefinition,
        search_path: str,
        marker_paths: set[str],
        platform_name: str,
    ) -> EngineObservation:
        executable_name: str | None = None
        executable_path: Path | None = None
        for name in definition.executable_names:
            observed = shutil.which(name, path=search_path)
            if observed is None:
                continue
            candidate = Path(observed).absolute()
            target = candidate.resolve(strict=True)
            if target.is_file() and os.access(candidate, os.X_OK):
                executable_name = name
                executable_path = candidate
                break
        detected = executable_path is not None
        platform_compatible = "*" in definition.platforms or platform_name in definition.platforms
        marker_names = {Path(path).name for path in marker_paths}
        project_used = any(
            marker in marker_paths
            or any(fnmatchcase(name, Path(marker).name) for name in marker_names)
            for marker in definition.project_markers
        )
        states = {
            "inventoried": AvailabilityState.TRUE,
            "detected": AvailabilityState.TRUE if detected else AvailabilityState.FALSE,
            "installed": AvailabilityState.TRUE if detected else AvailabilityState.UNKNOWN,
            "executable": AvailabilityState.TRUE if detected else AvailabilityState.UNKNOWN,
            "authenticated": AvailabilityState.UNKNOWN,
            "healthy": AvailabilityState.UNKNOWN,
            "project_used": AvailabilityState.TRUE if project_used else AvailabilityState.FALSE,
            "eligible": (
                AvailabilityState.TRUE
                if detected and platform_compatible
                else AvailabilityState.FALSE
            ),
            "authorized": AvailabilityState.UNKNOWN,
        }
        now = utc_now()
        facts = tuple(
            AvailabilityFact(
                dimension=dimension,
                state=states[dimension],
                evidence=(
                    f"executable:{executable_name}"
                    if dimension in {"detected", "installed", "executable"} and detected
                    else f"profile:{self._profile.profile_id}@{self._profile.revision}"
                ),
                source="machine-path-observation",
                observed_at=now,
                freshness_seconds=self._profile.freshness_seconds,
                sensitivity="machine_local",
            )
            for dimension in _DIMENSIONS
        )
        return EngineObservation(
            engine_id=definition.engine_id,
            adapter_id=definition.adapter_id,
            executable_name=executable_name,
            executable_path=executable_path,
            version=None,
            semantics=definition.semantics,
            platforms=definition.platforms,
            facts=facts,
            safe_probe=(
                None
                if executable_path is None
                else (str(executable_path), *definition.safe_version_arguments)
            ),
        )

    @staticmethod
    def _safe_read(root: Path, path: Path, maximum: int) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise MishkanError(ErrorCode.FILE, "environment descriptor is not a plain file")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise MishkanError(ErrorCode.FILE, "environment descriptor escapes the workspace")
        if resolved.stat().st_size > maximum:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "environment descriptor exceeds the configured byte bound",
            )
        return resolved.read_bytes()

    @staticmethod
    def _validate_pattern(pattern: str) -> None:
        pure = PurePosixPath(pattern)
        if pure.is_absolute() or ".." in pure.parts or "**" in pure.parts:
            raise MishkanError(ErrorCode.CONFIGURATION, "environment path pattern is unsafe")
