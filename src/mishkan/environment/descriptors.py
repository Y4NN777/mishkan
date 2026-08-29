"""Artifact-first validation for supported engineering descriptor formats."""

from __future__ import annotations

import configparser
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import yaml

from mishkan.artifacts import ArtifactLifecycle, ArtifactManifest
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.environment.models import (
    DescriptorValidationResult,
    EnvironmentBindingState,
    EnvironmentDescriptorMember,
    EnvironmentDescriptorSet,
    EnvironmentOutcome,
)
from mishkan.environment.repository import SQLiteEnvironmentRepository


class ArtifactReader(Protocol):
    def manifest(self, reference: str) -> ArtifactManifest: ...

    def read_bytes(self, reference: str) -> bytes: ...


class EnvironmentDescriptorValidator:
    """Validate descriptor syntax and binding lineage without applying project changes."""

    validator_revision = "mishkan.environment-descriptor@1"

    def __init__(
        self,
        repository: SQLiteEnvironmentRepository,
        artifacts: ArtifactReader,
        *,
        max_descriptor_bytes: int,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._max_descriptor_bytes = max_descriptor_bytes

    def validate(self, descriptor_set: EnvironmentDescriptorSet) -> DescriptorValidationResult:
        binding = self._repository.binding(str(descriptor_set.binding_id))
        if binding.state is not EnvironmentBindingState.COMPATIBLE:
            raise MishkanError(
                ErrorCode.ENGINEERING,
                "descriptor validation requires a compatible environment binding",
            )
        observation = self._repository.observation(str(binding.request.observation_id))
        violations: list[str] = []
        if descriptor_set.context_fingerprint != observation.fingerprint:
            violations.append("context-fingerprint-mismatch")
        if descriptor_set.target_platform != binding.request.target_platform:
            violations.append("target-platform-mismatch")
        if descriptor_set.target_architecture != binding.request.target_architecture:
            violations.append("target-architecture-mismatch")
        existing = {item.logical_path: item for item in observation.descriptors}
        member_paths = {item.logical_path for item in descriptor_set.members}
        validated: list[str] = []
        for member in descriptor_set.members:
            member_violations = self._validate_member(
                member,
                binding.request.allowed_descriptor_formats,
            )
            violations.extend(f"{member.logical_path}:{item}" for item in member_violations)
            if (
                member.base_revision is not None
                and member.base_revision != observation.repository_revision
            ):
                violations.append(f"{member.logical_path}:stale-base-revision")
            try:
                manifest = self._artifacts.manifest(member.artifact_reference)
                content = self._artifacts.read_bytes(member.artifact_reference)
            except MishkanError:
                violations.append(f"{member.logical_path}:artifact-unavailable")
                continue
            if manifest.lifecycle is not ArtifactLifecycle.AVAILABLE:
                violations.append(f"{member.logical_path}:artifact-not-available")
                continue
            if len(content) > self._max_descriptor_bytes:
                violations.append(f"{member.logical_path}:descriptor-size-bound")
                continue
            prior = existing.get(member.logical_path)
            if binding.request.requested_outcome is EnvironmentOutcome.REUSE_EXISTING:
                if prior is None or prior.digest != manifest.digest:
                    violations.append(f"{member.logical_path}:existing-definition-digest-mismatch")
            elif prior is not None and prior.digest != manifest.digest:
                violations.append(f"{member.logical_path}:existing-definition-protected")
            try:
                self._validate_content(member.format, content)
            except MishkanError as error:
                violations.append(
                    f"{member.logical_path}:{error.envelope.details.get('category', 'invalid')}"
                )
            else:
                validated.append(member.logical_path)
                for reference in self._referenced_paths(member.format, content):
                    normalized_reference = (Path(member.logical_path).parent / reference).as_posix()
                    if normalized_reference not in member_paths:
                        violations.append(
                            f"{member.logical_path}:referenced-input-missing:{normalized_reference}"
                        )
        return DescriptorValidationResult(
            descriptor_set_id=descriptor_set.descriptor_set_id,
            binding_id=descriptor_set.binding_id,
            valid=not violations,
            validated_members=tuple(validated),
            violations=tuple(dict.fromkeys(violations)),
            validator_revision=self.validator_revision,
        )

    @staticmethod
    def _validate_member(
        member: EnvironmentDescriptorMember,
        allowed_formats: tuple[str, ...],
    ) -> tuple[str, ...]:
        path = Path(member.logical_path)
        violations = []
        if path.is_absolute() or ".." in path.parts or "\\" in member.logical_path:
            violations.append("unsafe-logical-path")
        if allowed_formats and member.format not in allowed_formats:
            violations.append("format-not-authorized-by-binding")
        return tuple(violations)

    def _validate_content(self, format_name: str, content: bytes) -> None:
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise self._invalid("encoding") from exc
        try:
            if format_name == "devcontainer":
                self._devcontainer(self._jsonc(text))
            elif format_name == "compose":
                self._compose(yaml.safe_load(text))
            elif format_name == "podman_kube":
                self._podman_kube(tuple(yaml.safe_load_all(text)))
            elif format_name == "containerfile":
                self._containerfile(text)
            elif format_name == "quadlet":
                self._quadlet(text)
            elif format_name == "nix":
                if not text.strip():
                    raise self._invalid("empty-document")
            elif format_name == "mise":
                import tomllib

                parsed = tomllib.loads(text)
                if not isinstance(parsed, dict):
                    raise self._invalid("document-shape")
            else:
                raise self._invalid("unsupported-format")
        except MishkanError:
            raise
        except (
            configparser.Error,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            yaml.YAMLError,
        ) as exc:
            raise self._invalid("syntax") from exc

    @staticmethod
    def _devcontainer(document: Any) -> None:
        if not isinstance(document, dict):
            raise EnvironmentDescriptorValidator._invalid("document-shape")
        choices = [name for name in ("image", "build", "dockerComposeFile") if name in document]
        if len(choices) != 1:
            raise EnvironmentDescriptorValidator._invalid("devcontainer-source-selection")
        if "build" in document and not isinstance(document["build"], (str, dict)):
            raise EnvironmentDescriptorValidator._invalid("devcontainer-build-shape")
        if isinstance(document.get("build"), dict):
            build = document["build"]
            for key in ("dockerfile", "context"):
                if key in build and (
                    not isinstance(build[key], str)
                    or not EnvironmentDescriptorValidator._relative(build[key])
                ):
                    raise EnvironmentDescriptorValidator._invalid(f"devcontainer-build-{key}-path")
        if "dockerComposeFile" in document:
            values = document["dockerComposeFile"]
            values = [values] if isinstance(values, str) else values
            if (
                not isinstance(values, list)
                or not values
                or not all(
                    isinstance(item, str) and EnvironmentDescriptorValidator._relative(item)
                    for item in values
                )
            ):
                raise EnvironmentDescriptorValidator._invalid("devcontainer-compose-path")

    @staticmethod
    def _compose(document: Any) -> None:
        if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
            raise EnvironmentDescriptorValidator._invalid("compose-services")
        if not document["services"]:
            raise EnvironmentDescriptorValidator._invalid("compose-empty-services")

    @staticmethod
    def _podman_kube(document: Any) -> None:
        documents = document if isinstance(document, list) else [document]
        if not documents or any(
            not isinstance(item, dict) or not item.get("apiVersion") or not item.get("kind")
            for item in documents
        ):
            raise EnvironmentDescriptorValidator._invalid("kubernetes-object")

    @staticmethod
    def _containerfile(text: str) -> None:
        instructions = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not instructions or not any(line.upper().startswith("FROM ") for line in instructions):
            raise EnvironmentDescriptorValidator._invalid("containerfile-from")

    @staticmethod
    def _quadlet(text: str) -> None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(text)
        if not set(parser.sections()) & {"Container", "Pod", "Kube"}:
            raise EnvironmentDescriptorValidator._invalid("quadlet-section")

    @staticmethod
    def _jsonc(text: str) -> Mapping[str, Any]:
        without_comments: list[str] = []
        index = 0
        quoted = False
        escaped = False
        while index < len(text):
            char = text[index]
            following = text[index + 1] if index + 1 < len(text) else ""
            if quoted:
                without_comments.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                index += 1
                continue
            if char == '"':
                quoted = True
                without_comments.append(char)
                index += 1
            elif char == "/" and following == "/":
                index += 2
                while index < len(text) and text[index] not in "\r\n":
                    index += 1
            elif char == "/" and following == "*":
                end = text.find("*/", index + 2)
                if end < 0:
                    raise EnvironmentDescriptorValidator._invalid("jsonc-comment")
                index = end + 2
            else:
                without_comments.append(char)
                index += 1
        normalized = re.sub(r",\s*([}\]])", r"\1", "".join(without_comments))
        parsed = json.loads(normalized)
        if not isinstance(parsed, dict):
            raise EnvironmentDescriptorValidator._invalid("document-shape")
        return parsed

    @staticmethod
    def _relative(value: str) -> bool:
        path = Path(value)
        return bool(value) and not path.is_absolute() and ".." not in path.parts

    @staticmethod
    def _invalid(category: str) -> MishkanError:
        return MishkanError(
            ErrorCode.ENGINEERING,
            "environment descriptor validation failed",
            details={"category": category},
        )

    def _referenced_paths(self, format_name: str, content: bytes) -> tuple[str, ...]:
        if format_name != "devcontainer":
            return ()
        try:
            document = self._jsonc(content.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, MishkanError):
            return ()
        references: list[str] = []
        compose = document.get("dockerComposeFile")
        if isinstance(compose, str):
            references.append(compose)
        elif isinstance(compose, list):
            references.extend(item for item in compose if isinstance(item, str))
        build = document.get("build")
        if isinstance(build, dict) and isinstance(build.get("dockerfile"), str):
            references.append(str(build["dockerfile"]))
        return tuple(dict.fromkeys(references))
