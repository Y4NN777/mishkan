"""Configured credential and content inspection before persistence or downstream use."""

from __future__ import annotations

import mmap
import os
import re
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.schema import SchemaRegistry
from mishkan.domain.sources import resolve_source_path


class InspectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InspectionAction(StrEnum):
    BLOCK = "block"
    REDACT = "redact"


class InspectionRule(InspectionModel):
    rule_id: str = Field(min_length=1)
    pattern: str = Field(min_length=1)
    action: InspectionAction
    replacement: str = "[REDACTED]"


class InspectionProfile(InspectionModel):
    schema_version: str = "1.0"
    profile_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    adoption_authority: str = Field(min_length=1)
    rules: tuple[InspectionRule, ...]


class ContentInspector:
    def __init__(self, profile: InspectionProfile) -> None:
        self.profile = profile
        try:
            self._compiled = tuple((rule, re.compile(rule.pattern)) for rule in profile.rules)
            self._compiled_bytes = tuple(
                (rule, re.compile(rule.pattern.encode("utf-8"))) for rule in profile.rules
            )
        except re.error as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "inspection profile contains an invalid expression",
                details={"profile_id": profile.profile_id},
            ) from exc

    def inspect(self, content: str, resolved_secrets: tuple[str, ...] = ()) -> str:
        if any(secret and secret in content for secret in resolved_secrets):
            raise MishkanError(
                ErrorCode.SECRET_CONTENT,
                "resolved credential content reached an inspection boundary",
                details={"profile_id": self.profile.profile_id},
            )
        inspected = content
        for rule, expression in self._compiled:
            if not expression.search(inspected):
                continue
            if rule.action is InspectionAction.BLOCK:
                raise MishkanError(
                    ErrorCode.SECRET_CONTENT,
                    "configured inspection rule blocked content",
                    details={"profile_id": self.profile.profile_id, "rule_id": rule.rule_id},
                )
            inspected = expression.sub(rule.replacement, inspected)
        return inspected

    def require_safe_file(
        self,
        path: Path,
        resolved_secrets: tuple[str, ...] = (),
    ) -> None:
        """Reject immutable content containing a configured or resolved secret.

        Artifact content cannot be redacted in place because doing so would invalidate its
        accepted digest. The byte-oriented scan operates on a read-only mapping, keeping memory
        bounded even for the largest configured artifact.
        """
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            size = os.fstat(descriptor).st_size
            if size == 0:
                return
            with mmap.mmap(descriptor, 0, access=mmap.ACCESS_READ) as content:
                for secret in resolved_secrets:
                    if secret and content.find(secret.encode("utf-8")) >= 0:
                        raise MishkanError(
                            ErrorCode.SECRET_CONTENT,
                            "resolved credential content reached an inspection boundary",
                            details={"profile_id": self.profile.profile_id},
                        )
                for rule, expression in self._compiled_bytes:
                    if expression.search(content) is None:
                        continue
                    raise MishkanError(
                        ErrorCode.SECRET_CONTENT,
                        "configured inspection rule blocked immutable artifact content",
                        details={
                            "profile_id": self.profile.profile_id,
                            "rule_id": rule.rule_id,
                        },
                    )
        finally:
            os.close(descriptor)


class InspectionProfileLoader:
    def load(self, uri: str, project_root: Path) -> InspectionProfile:
        raw = self._read(uri, project_root)
        try:
            document: Any = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "inspection profile is malformed YAML",
                details={"source": uri},
            ) from exc
        if not isinstance(document, dict):
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "inspection profile must contain a mapping",
                details={"source": uri},
            )
        SchemaRegistry.require_supported("mishkan.inspection", document.get("schema_version"))
        try:
            return InspectionProfile.model_validate(document)
        except ValidationError as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "inspection profile is invalid",
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
                    "package inspection URI must identify a module and resource",
                    details={"source": uri},
                )
            return files(module).joinpath(resource).read_bytes()
        path = resolve_source_path(uri, project_root, "inspection profile")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "inspection profile cannot be read",
                details={"source": uri, "reason": type(exc).__name__},
            ) from exc
