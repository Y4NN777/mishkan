"""Deferred tool discovery, toolset resolution, availability, and immutable snapshots."""

from __future__ import annotations

import hashlib
import json
import platform
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.schema import SchemaRegistry
from mishkan.domain.sources import resolve_source_path
from mishkan.tools.models import (
    AvailabilityResult,
    AvailabilityState,
    RegistrySnapshot,
    ToolBinding,
    ToolContract,
    ToolMetadata,
    ToolsetDefinition,
    ToolSourceIndex,
)


class ToolCatalog:
    def __init__(
        self,
        source_uris: tuple[str, ...],
        project_root: Path,
        *,
        runtime: str = "python",
        available_services: frozenset[str] = frozenset(),
        available_dependencies: frozenset[str] = frozenset(),
        available_credentials: frozenset[str] = frozenset(),
        available_adapters: frozenset[str] = frozenset(),
        memory_mb: int | None = None,
    ) -> None:
        if not source_uris:
            raise MishkanError(
                ErrorCode.TOOL_CONTRACT,
                "at least one configured tool source is required",
            )
        self._source_uris = source_uris
        self._project_root = project_root.resolve()
        self._runtime = runtime
        self._services = available_services
        self._dependencies = available_dependencies
        self._credentials = available_credentials
        self._adapters = available_adapters
        self._memory_mb = memory_mb
        self._indices = tuple(self._load_index(uri) for uri in source_uris)
        self._validate_collisions()

    def list_metadata(self) -> tuple[ToolMetadata, ...]:
        return tuple(tool for index in self._indices for tool in index.tools)

    def search(self, query: str) -> tuple[ToolMetadata, ...]:
        normalized = query.casefold()
        return tuple(
            tool
            for tool in self.list_metadata()
            if normalized in tool.tool_id.casefold() or normalized in tool.summary.casefold()
        )

    def availability(self, metadata: ToolMetadata) -> AvailabilityResult:
        conditions = metadata.availability
        missing: list[str] = []
        system = platform.system().lower()
        if "*" not in conditions.platforms and system not in conditions.platforms:
            missing.append(f"platform:{system}")
        if "*" not in conditions.runtimes and self._runtime not in conditions.runtimes:
            missing.append(f"runtime:{self._runtime}")
        missing.extend(
            f"credential:{item}"
            for item in conditions.credential_refs
            if item not in self._credentials
        )
        missing.extend(
            f"service:{item}" for item in conditions.services if item not in self._services
        )
        missing.extend(
            f"dependency:{item}"
            for item in conditions.dependencies
            if item not in self._dependencies
        )
        if conditions.min_memory_mb is not None and (
            self._memory_mb is None or self._memory_mb < conditions.min_memory_mb
        ):
            missing.append(f"memory_mb:{conditions.min_memory_mb}")
        return AvailabilityResult(
            tool_id=metadata.tool_id,
            state=AvailabilityState.UNAVAILABLE if missing else AvailabilityState.AVAILABLE,
            missing_conditions=tuple(sorted(missing)),
        )

    def snapshot(self, requested: tuple[str, ...]) -> RegistrySnapshot:
        tool_ids = self._resolve_requested(requested)
        metadata_by_id = {tool.tool_id: tool for tool in self.list_metadata()}
        contracts: list[ToolContract] = []
        for tool_id in tool_ids:
            metadata = metadata_by_id.get(tool_id)
            if metadata is None:
                raise MishkanError(
                    ErrorCode.TOOL_CONTRACT,
                    "requested tool is not present in configured sources",
                    details={"tool_id": tool_id},
                )
            availability = self.availability(metadata)
            if availability.state is AvailabilityState.UNAVAILABLE:
                raise MishkanError(
                    ErrorCode.TOOL_UNAVAILABLE,
                    "requested tool is unavailable",
                    details={
                        "tool_id": tool_id,
                        "missing_conditions": availability.missing_conditions,
                    },
                )
            contract = self._load_contract(metadata.contract_uri)
            if (
                contract.tool_id != metadata.tool_id
                or contract.version != metadata.version
                or contract.source_id != metadata.source_id
                or contract.effect_class is not metadata.effect_class
            ):
                raise MishkanError(
                    ErrorCode.TOOL_DRIFT,
                    "tool contract differs from deferred catalogue metadata",
                    details={"tool_id": tool_id, "source_id": metadata.source_id},
                )
            if contract.adapter not in self._adapters:
                raise MishkanError(
                    ErrorCode.TOOL_UNAVAILABLE,
                    "requested tool adapter is unavailable",
                    details={
                        "tool_id": tool_id,
                        "missing_conditions": (f"adapter:{contract.adapter}",),
                    },
                )
            contracts.append(contract)
        resolved_toolsets = tuple(
            (item, self._resolve_toolset(item, set(), 0))
            for item in requested
            if self._toolset(item) is not None
        )
        source_fingerprints = tuple(
            sorted((index.source_id, index.fingerprint) for index in self._indices)
        )
        payload = {
            "tools": [tool.model_dump(mode="json") for tool in contracts],
            "toolsets": resolved_toolsets,
            "sources": source_fingerprints,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return RegistrySnapshot(
            tools=tuple(contracts),
            resolved_toolsets=resolved_toolsets,
            source_fingerprints=source_fingerprints,
            fingerprint=fingerprint,
        )

    @staticmethod
    def bind(
        snapshot: RegistrySnapshot,
        task_id: str,
        role: str,
        tool_id: str,
        allowed_targets: tuple[str, ...],
        allowed_call_fingerprints: tuple[str, ...] = (),
    ) -> ToolBinding:
        tool = snapshot.require(tool_id)
        return ToolBinding(
            task_id=task_id,
            role=role,
            tool_id=tool.tool_id,
            tool_version=tool.version,
            contract_fingerprint=tool.provenance_fingerprint,
            registry_fingerprint=snapshot.fingerprint,
            allowed_targets=allowed_targets,
            allowed_call_fingerprints=allowed_call_fingerprints,
        )

    def _resolve_requested(self, requested: tuple[str, ...]) -> tuple[str, ...]:
        resolved: list[str] = []
        known_tools = {tool.tool_id for tool in self.list_metadata()}
        for item in requested:
            if item in known_tools:
                if item not in resolved:
                    resolved.append(item)
                continue
            toolset = self._toolset(item)
            if toolset is None:
                raise MishkanError(
                    ErrorCode.TOOL_CONTRACT,
                    "requested tool or toolset is unknown",
                    details={"identity": item},
                )
            for tool_id in self._resolve_toolset(item, set(), 0):
                if tool_id not in resolved:
                    resolved.append(tool_id)
        return tuple(resolved)

    def _resolve_toolset(self, toolset_id: str, stack: set[str], depth: int) -> tuple[str, ...]:
        toolset = self._toolset(toolset_id)
        if toolset is None:
            raise MishkanError(
                ErrorCode.TOOL_CONTRACT,
                "nested toolset is unknown",
                details={"toolset_id": toolset_id},
            )
        if toolset_id in stack:
            raise MishkanError(
                ErrorCode.TOOL_CONTRACT,
                "toolset resolution cycle detected",
                details={"toolset_id": toolset_id, "stack": sorted(stack)},
            )
        if depth >= toolset.max_depth:
            raise MishkanError(
                ErrorCode.TOOL_CONTRACT,
                "toolset nesting bound exceeded",
                details={"toolset_id": toolset_id, "max_depth": toolset.max_depth},
            )
        next_stack = {*stack, toolset_id}
        resolved = list(toolset.tools)
        for nested in toolset.toolsets:
            resolved.extend(self._resolve_toolset(nested, next_stack, depth + 1))
        excluded = set(toolset.exclude_tools)
        return tuple(dict.fromkeys(item for item in resolved if item not in excluded))

    def _toolset(self, toolset_id: str) -> ToolsetDefinition | None:
        matches = [
            toolset
            for index in self._indices
            for toolset in index.toolsets
            if toolset.toolset_id == toolset_id
        ]
        if len(matches) > 1:
            raise MishkanError(
                ErrorCode.TOOL_DRIFT,
                "toolset identity collides across configured sources",
                details={"toolset_id": toolset_id},
            )
        return matches[0] if matches else None

    def _validate_collisions(self) -> None:
        claims: dict[str, list[str]] = {}
        toolset_claims: dict[str, list[str]] = {}
        for index in self._indices:
            for tool in index.tools:
                claims.setdefault(tool.tool_id, []).append(index.source_id)
            for toolset in index.toolsets:
                toolset_claims.setdefault(toolset.toolset_id, []).append(index.source_id)
        collisions = {
            identity: sources
            for identity, sources in {**claims, **toolset_claims}.items()
            if len(sources) > 1
        }
        if collisions:
            raise MishkanError(
                ErrorCode.TOOL_DRIFT,
                "tool or toolset identities collide across configured sources",
                details={"collisions": collisions},
            )

    def _load_index(self, uri: str) -> ToolSourceIndex:
        document = self._read_yaml(uri)
        SchemaRegistry.require_supported("mishkan.tool-source", document.get("schema_version"))
        try:
            return ToolSourceIndex.model_validate(document)
        except ValidationError as exc:
            raise MishkanError(
                ErrorCode.TOOL_CONTRACT,
                "configured tool source index is invalid",
                details={"source": uri, "violations": len(exc.errors())},
            ) from exc

    def _load_contract(self, uri: str) -> ToolContract:
        document = self._read_yaml(uri)
        SchemaRegistry.require_supported("mishkan.tool", document.get("schema_version"))
        try:
            return ToolContract.model_validate(document)
        except ValidationError as exc:
            raise MishkanError(
                ErrorCode.TOOL_CONTRACT,
                "configured tool contract is invalid",
                details={"source": uri, "violations": len(exc.errors())},
            ) from exc

    def _read_yaml(self, uri: str) -> dict[str, Any]:
        raw = self._read(uri)
        try:
            document: Any = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise MishkanError(
                ErrorCode.TOOL_CONTRACT,
                "tool source is malformed YAML",
                details={"source": uri},
            ) from exc
        if not isinstance(document, dict):
            raise MishkanError(
                ErrorCode.TOOL_CONTRACT,
                "tool source must contain a mapping",
                details={"source": uri},
            )
        return document

    def _read(self, uri: str) -> bytes:
        if uri.startswith("package://"):
            location = uri.removeprefix("package://")
            module, separator, resource = location.partition("/")
            if not separator or not module or not resource:
                raise MishkanError(
                    ErrorCode.TOOL_CONTRACT,
                    "package tool URI must identify a module and resource",
                    details={"source": uri},
                )
            try:
                return files(module).joinpath(resource).read_bytes()
            except (ModuleNotFoundError, OSError) as exc:
                raise MishkanError(
                    ErrorCode.TOOL_CONTRACT,
                    "package tool source cannot be read",
                    details={"source": uri, "reason": type(exc).__name__},
                ) from exc
        path = resolve_source_path(uri, self._project_root, "tool source")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise MishkanError(
                ErrorCode.TOOL_CONTRACT,
                "tool source cannot be read",
                details={"source": uri, "reason": type(exc).__name__},
            ) from exc
