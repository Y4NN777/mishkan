"""Bounded local skill discovery, selection, and progressive content loading."""

from __future__ import annotations

import hashlib
import platform as host_platform
import re
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.sources import resolve_source_path
from mishkan.skills.models import (
    SkillActivationState,
    SkillBounds,
    SkillBundleDefinition,
    SkillBundleMode,
    SkillBundleResolution,
    SkillLoadedResource,
    SkillLoadEvidence,
    SkillMetadata,
    SkillSelection,
    SkillSelectionContext,
    SkillSourceDefinition,
    SkillSourceKind,
    SkillTrustState,
    SkillUseOutcome,
)

_FRONTMATTER_KEYS = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)
_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class LoadedSkill:
    metadata: SkillMetadata
    instructions: str
    evidence: SkillLoadEvidence
    resources: tuple[tuple[SkillLoadedResource, bytes], ...] = ()


class SkillCatalog:
    """Resolve configured local sources without treating discovery as authority."""

    def __init__(
        self,
        sources: tuple[SkillSourceDefinition, ...],
        project_root: Path,
        *,
        bounds: SkillBounds,
    ) -> None:
        if not sources:
            raise MishkanError(ErrorCode.SKILL_CONTRACT, "at least one skill source is required")
        self._project_root = project_root.resolve()
        self._bounds = bounds
        self._packages: dict[str, Path] = {}
        discovered: list[tuple[int, SkillMetadata]] = []
        for source in sources:
            if source.enabled:
                discovered.extend(self._discover_source(source))
        self._metadata = self._resolve_precedence(discovered)

    def list_metadata(self) -> tuple[SkillMetadata, ...]:
        return tuple(sorted(self._metadata.values(), key=lambda item: item.name))

    def search(self, query: str) -> tuple[SkillMetadata, ...]:
        normalized = query.casefold()
        return tuple(
            item
            for item in self.list_metadata()
            if normalized in item.name.casefold() or normalized in item.description.casefold()
        )

    def select(self, name: str, context: SkillSelectionContext) -> SkillSelection:
        return select_skill_metadata(name, self._metadata.get(name), context)

    def load_instructions(self, selection: SkillSelection) -> LoadedSkill:
        metadata = selection.selected
        if metadata is None:
            raise MishkanError(
                ErrorCode.SKILL_SELECTION,
                "skill instructions cannot be loaded for a miss",
                details={"skill": selection.requested_name},
            )
        path = self._packages[metadata.package_uri] / "SKILL.md"
        content = self._read_bounded(path, self._bounds.max_manifest_bytes, "skill manifest")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MishkanError(
                ErrorCode.SKILL_CONTRACT,
                "SKILL.md must be UTF-8 text",
                details={"skill": metadata.name},
            ) from exc
        frontmatter, body = self._parse_document(text, path)
        if frontmatter.get("name") != metadata.name or not body.strip():
            raise MishkanError(
                ErrorCode.SKILL_CONTRACT,
                "loaded SKILL.md differs from catalogue metadata or has no instructions",
                details={"skill": metadata.name},
            )
        observed_package = self._package_fingerprint(path.parent)[0]
        if observed_package != metadata.package_fingerprint:
            raise MishkanError(
                ErrorCode.SKILL_SELECTION,
                "skill package changed after catalogue selection",
                details={"skill": metadata.name},
            )
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        evidence = SkillLoadEvidence(
            task_id=selection.context.task_id,
            task_class=selection.context.task_class,
            consuming_identity=selection.context.consuming_identity,
            skill_name=metadata.name,
            skill_version=metadata.version,
            package_fingerprint=metadata.package_fingerprint,
            outcome=selection.outcome,
            reason=selection.reason,
            instruction_fingerprint=digest,
        )
        return LoadedSkill(metadata=metadata, instructions=text, evidence=evidence)

    def resolve_bundle(
        self,
        bundle: SkillBundleDefinition,
        context: SkillSelectionContext,
    ) -> SkillBundleResolution:
        selections = tuple(self.select(name, context) for name in bundle.skills)
        eligible = tuple(
            selection for selection in selections if selection.outcome is not SkillUseOutcome.MISS
        )
        if bundle.mode is SkillBundleMode.ALL:
            if len(eligible) != len(selections):
                return SkillBundleResolution(
                    bundle_id=bundle.bundle_id,
                    bundle_version=bundle.version,
                    context=context,
                    outcome=SkillUseOutcome.MISS,
                    selections=selections,
                    selected_skill_names=(),
                    reason="all-mode bundle contains an ineligible skill",
                )
            outcome = (
                SkillUseOutcome.PARTIAL
                if any(item.outcome is SkillUseOutcome.PARTIAL for item in eligible)
                else SkillUseOutcome.HIT
            )
            chosen = eligible
        else:
            limit = bundle.max_selected
            if limit is None:
                raise AssertionError("validated select-mode bundle has no bound")
            chosen = eligible[:limit]
            if not chosen:
                return SkillBundleResolution(
                    bundle_id=bundle.bundle_id,
                    bundle_version=bundle.version,
                    context=context,
                    outcome=SkillUseOutcome.MISS,
                    selections=selections,
                    selected_skill_names=(),
                    reason="select-mode bundle has no eligible skill",
                )
            outcome = (
                SkillUseOutcome.PARTIAL
                if len(eligible) < len(selections)
                or any(item.outcome is SkillUseOutcome.PARTIAL for item in chosen)
                else SkillUseOutcome.HIT
            )
        return SkillBundleResolution(
            bundle_id=bundle.bundle_id,
            bundle_version=bundle.version,
            context=context,
            outcome=outcome,
            selections=selections,
            selected_skill_names=tuple(
                item.selected.name for item in chosen if item.selected is not None
            ),
            reason="bundle resolved from explicit skill order and task compatibility",
        )

    def load_resource(self, loaded: LoadedSkill, relative_path: str) -> LoadedSkill:
        normalized = self._safe_resource_path(relative_path)
        if normalized not in loaded.metadata.resource_paths:
            raise MishkanError(
                ErrorCode.SKILL_CONTRACT,
                "skill resource is not part of the selected package",
                details={"skill": loaded.metadata.name, "path": normalized},
            )
        if normalized not in loaded.instructions:
            raise MishkanError(
                ErrorCode.SKILL_CONTRACT,
                "skill resource was not referenced by the loaded instructions",
                details={"skill": loaded.metadata.name, "path": normalized},
            )
        if any(item.path == normalized for item, _ in loaded.resources):
            return loaded
        root = self._packages[loaded.metadata.package_uri]
        path = (root / normalized).resolve()
        if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
            raise MishkanError(
                ErrorCode.SKILL_CONTRACT,
                "skill resource does not resolve to a package file",
                details={"skill": loaded.metadata.name, "path": normalized},
            )
        content = self._read_bounded(path, self._bounds.max_resource_bytes, "skill resource")
        record = SkillLoadedResource(
            path=normalized,
            fingerprint=f"sha256:{hashlib.sha256(content).hexdigest()}",
            size_bytes=len(content),
        )
        resources = (*loaded.resources, (record, content))
        evidence = loaded.evidence.model_copy(
            update={"loaded_resources": tuple(item for item, _ in resources)}
        )
        return replace(loaded, evidence=evidence, resources=resources)

    def _discover_source(self, source: SkillSourceDefinition) -> list[tuple[int, SkillMetadata]]:
        if source.kind in {SkillSourceKind.URL, SkillSourceKind.HUB}:
            raise MishkanError(
                ErrorCode.SKILL_CONTRACT,
                "remote skill source requires an acquired local provenance lock",
                details={"source_id": source.source_id, "kind": source.kind.value},
            )
        root = resolve_source_path(source.uri, self._project_root, "skill source")
        if root.is_symlink() or not root.is_dir():
            raise MishkanError(
                ErrorCode.SKILL_CONTRACT,
                "configured skill source is not a concrete directory",
                details={"source_id": source.source_id},
            )
        discovered: list[tuple[int, SkillMetadata]] = []
        for package in sorted(root.iterdir(), key=lambda path: path.name):
            if package.is_symlink() or not package.is_dir():
                continue
            manifest = package / "SKILL.md"
            if not manifest.is_file() or manifest.is_symlink():
                continue
            metadata = self._metadata_from_manifest(source, package, manifest)
            package_uri = metadata.package_uri
            self._packages[package_uri] = package.resolve()
            discovered.append((source.precedence, metadata))
        return discovered

    def _metadata_from_manifest(
        self,
        source: SkillSourceDefinition,
        package: Path,
        manifest: Path,
    ) -> SkillMetadata:
        header = self._read_frontmatter(manifest)
        document = self._parse_frontmatter(header, manifest)
        name = document.get("name")
        description = document.get("description")
        if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name) or len(name) > 64:
            raise self._contract_error("skill name is invalid", manifest)
        if name != package.name:
            raise self._contract_error("skill name must match its directory", manifest)
        if not isinstance(description, str) or not description.strip() or len(description) > 1_024:
            raise self._contract_error("skill description is invalid", manifest)
        extra = set(document) - _FRONTMATTER_KEYS
        if extra and not self._bounds.allow_frontmatter_extensions:
            raise self._contract_error("SKILL.md contains unsupported frontmatter fields", manifest)
        metadata = document.get("metadata", {})
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise self._contract_error("skill metadata must be a mapping", manifest)
        version = metadata.get("version")
        if not isinstance(version, str):
            raise self._contract_error("skill metadata.version is required", manifest)
        mishkan = metadata.get("mishkan", {})
        if mishkan is None:
            mishkan = {}
        if not isinstance(mishkan, dict):
            raise self._contract_error("skill metadata.mishkan must be a mapping", manifest)
        fingerprint, resources = self._package_fingerprint(package)
        package_uri = f"{source.source_id}:{name}@{source.revision}"
        activation = source.package_activation.get(name, source.default_activation)
        trust = source.trust
        if trust is SkillTrustState.QUARANTINED:
            activation = SkillActivationState.QUARANTINED
        return SkillMetadata(
            name=name,
            description=description.strip(),
            version=version,
            source_id=source.source_id,
            source_kind=source.kind,
            source_revision=source.revision,
            package_uri=package_uri,
            package_fingerprint=fingerprint,
            trust=trust,
            activation=activation,
            license=self._optional_string(document, "license"),
            compatibility_summary=self._optional_string(document, "compatibility"),
            allowed_tools_hint=self._optional_string(document, "allowed-tools"),
            author_claim=self._optional_string(metadata, "author"),
            platforms=self._string_tuple(mishkan, "platforms", ("*",)),
            required_tools=self._string_tuple(mishkan, "required_tools", ()),
            fallback_tools=self._fallback_mapping(mishkan),
            organization_versions=self._string_tuple(mishkan, "organization_versions", ("*",)),
            task_classes=self._string_tuple(mishkan, "task_classes", ()),
            resource_paths=resources,
        )

    def _resolve_precedence(
        self,
        discovered: list[tuple[int, SkillMetadata]],
    ) -> dict[str, SkillMetadata]:
        grouped: dict[str, list[tuple[int, SkillMetadata]]] = {}
        for item in discovered:
            grouped.setdefault(item[1].name, []).append(item)
        resolved: dict[str, SkillMetadata] = {}
        for name, candidates in grouped.items():
            ordered = sorted(candidates, key=lambda item: (-item[0], item[1].source_id))
            if len(ordered) > 1 and ordered[0][0] == ordered[1][0]:
                raise MishkanError(
                    ErrorCode.SKILL_SELECTION,
                    "skill identity has equal-precedence source candidates",
                    details={
                        "skill": name,
                        "sources": sorted(
                            item[1].source_id for item in ordered if item[0] == ordered[0][0]
                        ),
                    },
                )
            resolved[name] = ordered[0][1]
        return resolved

    @staticmethod
    def _availability(metadata: SkillMetadata, context: SkillSelectionContext) -> tuple[str, ...]:
        missing: list[str] = []
        if metadata.activation is not SkillActivationState.ACTIVE:
            missing.append(f"activation:{metadata.activation.value}")
        if metadata.trust is SkillTrustState.QUARANTINED:
            missing.append("trust:quarantined")
        if "*" not in metadata.platforms and context.platform not in metadata.platforms:
            missing.append(f"platform:{context.platform}")
        if (
            "*" not in metadata.organization_versions
            and context.organization_version not in metadata.organization_versions
        ):
            missing.append(f"organization:{context.organization_version}")
        if metadata.task_classes and context.task_class not in metadata.task_classes:
            missing.append(f"task_class:{context.task_class}")
        return tuple(sorted(missing))

    def _package_fingerprint(self, package: Path) -> tuple[str, tuple[str, ...]]:
        files: list[tuple[str, bytes]] = []
        total = 0
        for path in sorted(package.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink():
                raise self._contract_error("skill package contains a symbolic link", path)
            if not path.is_file():
                continue
            relative = path.relative_to(package).as_posix()
            if len(PurePosixPath(relative).parts) - 1 > self._bounds.max_resource_depth:
                raise self._contract_error("skill resource exceeds configured depth", path)
            if len(files) >= self._bounds.max_package_files:
                raise self._contract_error("skill package exceeds configured file count", path)
            content = self._read_bounded(path, self._bounds.max_package_bytes, "skill package file")
            total += len(content)
            if total > self._bounds.max_package_bytes:
                raise self._contract_error("skill package exceeds configured byte bound", package)
            files.append((relative, content))
        digest = hashlib.sha256()
        for relative, content in files:
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(content).digest())
        resources = tuple(relative for relative, _ in files if relative != "SKILL.md")
        return f"sha256:{digest.hexdigest()}", resources

    def _read_frontmatter(self, manifest: Path) -> bytes:
        collected = bytearray()
        delimiter_count = 0
        try:
            with manifest.open("rb") as stream:
                for line in stream:
                    collected.extend(line)
                    if len(collected) > self._bounds.max_frontmatter_bytes:
                        raise self._contract_error(
                            "SKILL.md frontmatter exceeds configured bound", manifest
                        )
                    if line.rstrip(b"\r\n") == b"---":
                        delimiter_count += 1
                        if delimiter_count == 2:
                            return bytes(collected)
        except OSError as exc:
            raise self._contract_error("SKILL.md frontmatter could not be read", manifest) from exc
        raise self._contract_error("SKILL.md frontmatter is incomplete", manifest)

    def _parse_frontmatter(self, content: bytes, path: Path) -> dict[str, Any]:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise self._contract_error("SKILL.md frontmatter must be UTF-8", path) from exc
        frontmatter, _ = self._parse_document(text, path)
        return frontmatter

    def _parse_document(self, text: str, path: Path) -> tuple[dict[str, Any], str]:
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            raise self._contract_error("SKILL.md must start with YAML frontmatter", path)
        closing = next(
            (index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None
        )
        if closing is None:
            raise self._contract_error("SKILL.md frontmatter is incomplete", path)
        try:
            raw = yaml.safe_load("".join(lines[1:closing]))
        except yaml.YAMLError as exc:
            raise self._contract_error("SKILL.md frontmatter is invalid YAML", path) from exc
        if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
            raise self._contract_error("SKILL.md frontmatter must be a string-keyed mapping", path)
        return raw, "".join(lines[closing + 1 :])

    def _safe_resource_path(self, value: str) -> str:
        if "\\" in value or "\x00" in value:
            raise MishkanError(ErrorCode.SKILL_CONTRACT, "skill resource path is invalid")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise MishkanError(ErrorCode.SKILL_CONTRACT, "skill resource path is invalid")
        if len(path.parts) - 1 > self._bounds.max_resource_depth:
            raise MishkanError(ErrorCode.SKILL_CONTRACT, "skill resource path exceeds depth bound")
        return path.as_posix()

    @staticmethod
    def _read_bounded(path: Path, limit: int, label: str) -> bytes:
        try:
            size = path.stat().st_size
            if size > limit:
                raise MishkanError(
                    ErrorCode.SKILL_CONTRACT,
                    f"{label} exceeds configured byte bound",
                    details={"size_bytes": size, "limit": limit},
                )
            return path.read_bytes()
        except MishkanError:
            raise
        except OSError as exc:
            raise MishkanError(
                ErrorCode.SKILL_CONTRACT,
                f"{label} could not be read",
                details={"reason": type(exc).__name__},
            ) from exc

    @staticmethod
    def _optional_string(mapping: dict[str, Any], key: str) -> str | None:
        value = mapping.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise MishkanError(ErrorCode.SKILL_CONTRACT, f"skill {key} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _string_tuple(
        mapping: dict[str, Any], key: str, default: tuple[str, ...]
    ) -> tuple[str, ...]:
        value = mapping.get(key)
        if value is None:
            return default
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise MishkanError(ErrorCode.SKILL_CONTRACT, f"skill {key} must be a string list")
        return tuple(value)

    @staticmethod
    def _fallback_mapping(mapping: dict[str, Any]) -> dict[str, tuple[str, ...]]:
        value = mapping.get("fallback_tools", {})
        if not isinstance(value, dict):
            raise MishkanError(ErrorCode.SKILL_CONTRACT, "skill fallback_tools must be a mapping")
        result: dict[str, tuple[str, ...]] = {}
        for key, alternatives in value.items():
            if (
                not isinstance(key, str)
                or not isinstance(alternatives, list)
                or any(not isinstance(item, str) or not item for item in alternatives)
            ):
                raise MishkanError(ErrorCode.SKILL_CONTRACT, "skill fallback_tools is invalid")
            result[key] = tuple(alternatives)
        return result

    @staticmethod
    def _contract_error(message: str, path: Path) -> MishkanError:
        return MishkanError(
            ErrorCode.SKILL_CONTRACT,
            message,
            details={"path": str(path)},
        )


def validate_skill_metadata_document(metadata: SkillMetadata, content: bytes) -> str:
    """Verify client-supplied Level-0 metadata against the immutable SKILL.md bytes."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MishkanError(ErrorCode.SKILL_CONTRACT, "SKILL.md must be UTF-8 text") from exc
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise MishkanError(ErrorCode.SKILL_CONTRACT, "SKILL.md must start with YAML frontmatter")
    closing = next(
        (index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"),
        None,
    )
    if closing is None:
        raise MishkanError(ErrorCode.SKILL_CONTRACT, "SKILL.md frontmatter is incomplete")
    try:
        document = yaml.safe_load("".join(lines[1:closing]))
    except yaml.YAMLError as exc:
        raise MishkanError(
            ErrorCode.SKILL_CONTRACT, "SKILL.md frontmatter is invalid YAML"
        ) from exc
    if not isinstance(document, dict):
        raise MishkanError(ErrorCode.SKILL_CONTRACT, "SKILL.md frontmatter must be a mapping")
    declared_metadata = document.get("metadata") or {}
    if not isinstance(declared_metadata, dict):
        raise MishkanError(ErrorCode.SKILL_CONTRACT, "skill metadata must be a mapping")
    mishkan = declared_metadata.get("mishkan") or {}
    if not isinstance(mishkan, dict):
        raise MishkanError(ErrorCode.SKILL_CONTRACT, "skill metadata.mishkan must be a mapping")

    def strings(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        value = mishkan.get(key, list(default))
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise MishkanError(ErrorCode.SKILL_CONTRACT, f"skill {key} must be a string list")
        return tuple(value)

    raw_fallbacks = mishkan.get("fallback_tools", {})
    if not isinstance(raw_fallbacks, dict) or any(
        not isinstance(key, str)
        or not isinstance(value, list)
        or not all(isinstance(item, str) for item in value)
        for key, value in raw_fallbacks.items()
    ):
        raise MishkanError(ErrorCode.SKILL_CONTRACT, "skill fallback_tools is invalid")
    fallbacks = {str(key): tuple(value) for key, value in raw_fallbacks.items()}
    declared = {
        "name": document.get("name"),
        "description": document.get("description"),
        "version": declared_metadata.get("version"),
        "license": document.get("license"),
        "compatibility_summary": document.get("compatibility"),
        "allowed_tools_hint": document.get("allowed-tools"),
        "author_claim": declared_metadata.get("author"),
        "platforms": strings("platforms", ("*",)),
        "required_tools": strings("required_tools", ()),
        "fallback_tools": fallbacks,
        "organization_versions": strings("organization_versions", ("*",)),
        "task_classes": strings("task_classes", ()),
    }
    observed = {
        "name": metadata.name,
        "description": metadata.description,
        "version": metadata.version,
        "license": metadata.license,
        "compatibility_summary": metadata.compatibility_summary,
        "allowed_tools_hint": metadata.allowed_tools_hint,
        "author_claim": metadata.author_claim,
        "platforms": metadata.platforms,
        "required_tools": metadata.required_tools,
        "fallback_tools": metadata.fallback_tools,
        "organization_versions": metadata.organization_versions,
        "task_classes": metadata.task_classes,
    }
    if declared != observed:
        raise MishkanError(
            ErrorCode.SKILL_TRUST,
            "skill Level-0 metadata differs from immutable SKILL.md frontmatter",
        )
    body = "".join(lines[closing + 1 :])
    if not body.strip():
        raise MishkanError(ErrorCode.SKILL_CONTRACT, "SKILL.md has no operating instructions")
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def select_skill_metadata(
    requested_name: str,
    metadata: SkillMetadata | None,
    context: SkillSelectionContext,
) -> SkillSelection:
    """Apply the same compatibility rules to filesystem or durable metadata."""
    if metadata is None:
        return SkillSelection(
            requested_name=requested_name,
            context=context,
            outcome=SkillUseOutcome.MISS,
            reason="skill is not present in the configured catalogue",
            missing_conditions=(f"skill:{requested_name}",),
        )
    unavailable = SkillCatalog._availability(metadata, context)
    if unavailable:
        return SkillSelection(
            requested_name=requested_name,
            context=context,
            outcome=SkillUseOutcome.MISS,
            reason="skill is not eligible for this task context",
            missing_conditions=unavailable,
        )
    fallback_bindings: dict[str, str] = {}
    missing_tools: list[str] = []
    for tool in metadata.required_tools:
        if tool in context.available_tools:
            continue
        fallback = next(
            (
                candidate
                for candidate in metadata.fallback_tools.get(tool, ())
                if candidate in context.available_tools
            ),
            None,
        )
        if fallback is None:
            missing_tools.append(f"tool:{tool}")
        else:
            fallback_bindings[tool] = fallback
    if missing_tools:
        return SkillSelection(
            requested_name=requested_name,
            context=context,
            outcome=SkillUseOutcome.MISS,
            reason="skill required tools are unavailable",
            missing_conditions=tuple(sorted(missing_tools)),
        )
    outcome = SkillUseOutcome.PARTIAL if fallback_bindings else SkillUseOutcome.HIT
    return SkillSelection(
        requested_name=requested_name,
        context=context,
        outcome=outcome,
        selected=metadata,
        reason=("compatible fallback tool selected" if fallback_bindings else "skill is eligible"),
        fallback_bindings=fallback_bindings,
    )


def default_selection_context(
    *,
    task_id: str,
    task_class: str,
    consuming_identity: str,
    organization_version: str,
    available_tools: frozenset[str] = frozenset(),
) -> SkillSelectionContext:
    """Construct a context using the observed host platform, never a claimed package value."""

    return SkillSelectionContext(
        task_id=task_id,
        task_class=task_class,
        consuming_identity=consuming_identity,
        platform=host_platform.system().lower(),
        organization_version=organization_version,
        available_tools=available_tools,
    )
