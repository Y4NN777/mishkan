"""Bounded, configured inspection for immutable skill package candidates."""

from __future__ import annotations

import fnmatch
import hashlib
import re
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from pydantic import ValidationError

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.schema import SchemaRegistry
from mishkan.domain.sources import resolve_source_path
from mishkan.skills.models import (
    SkillFindingSeverity,
    SkillInspectionFinding,
    SkillInspectionProfile,
    SkillInspectionResult,
)

_SEVERITY_ORDER = {
    SkillFindingSeverity.INFO: 0,
    SkillFindingSeverity.LOW: 1,
    SkillFindingSeverity.MEDIUM: 2,
    SkillFindingSeverity.HIGH: 3,
    SkillFindingSeverity.CRITICAL: 4,
}


class SkillPackageInspector:
    """Apply a public versioned rule profile without exposing matched secret content."""

    def __init__(self, profile: SkillInspectionProfile) -> None:
        self.profile = profile
        try:
            self._rules = tuple((rule, re.compile(rule.pattern)) for rule in profile.rules)
        except re.error as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "skill inspection profile contains an invalid expression",
                details={"profile_id": profile.profile_id},
            ) from exc

    def inspect(
        self,
        package: Path,
        *,
        expected_fingerprint: str,
    ) -> SkillInspectionResult:
        root = package.resolve()
        if not root.is_dir():
            raise MishkanError(ErrorCode.SKILL_CONTRACT, "skill package is not a directory")
        entries: list[tuple[str, bytes]] = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink():
                raise MishkanError(
                    ErrorCode.SKILL_CONTRACT,
                    "skill package inspection refuses symbolic links",
                )
            if not path.is_file():
                continue
            logical_path = path.relative_to(root).as_posix()
            if any(part in {"", ".", ".."} for part in PurePosixPath(logical_path).parts):
                raise MishkanError(ErrorCode.SKILL_CONTRACT, "skill package path is invalid")
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise MishkanError(
                    ErrorCode.SKILL_CONTRACT,
                    "skill package content cannot be inspected",
                    details={"path": logical_path, "reason": type(exc).__name__},
                ) from exc
            entries.append((logical_path, content))
        return self.inspect_entries(dict(entries), expected_fingerprint=expected_fingerprint)

    def inspect_entries(
        self,
        entries: dict[str, bytes],
        *,
        expected_fingerprint: str,
    ) -> SkillInspectionResult:
        findings: list[SkillInspectionFinding] = []
        digest = hashlib.sha256()
        scanned_bytes = 0
        if len(entries) > self.profile.max_scanned_files:
            raise self._bounded_error("file count")
        for logical_path, content in sorted(entries.items()):
            path = PurePosixPath(logical_path)
            if (
                not logical_path
                or "\\" in logical_path
                or "\x00" in logical_path
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise MishkanError(ErrorCode.SKILL_CONTRACT, "skill package path is invalid")
            if len(content) > self.profile.max_scanned_file_bytes:
                raise self._bounded_error("file bytes")
            scanned_bytes += len(content)
            if scanned_bytes > self.profile.max_total_bytes:
                raise self._bounded_error("total bytes")
            digest.update(logical_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(content).digest())
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = content.decode("utf-8", errors="replace")
            for rule, expression in self._rules:
                if not any(
                    fnmatch.fnmatchcase(logical_path, pattern) for pattern in rule.path_patterns
                ):
                    continue
                for match in expression.finditer(text):
                    prefix = text[: match.start()]
                    evidence = hashlib.sha256(
                        f"{rule.rule_id}\0{logical_path}\0{match.start()}\0{match.end()}".encode()
                    ).hexdigest()
                    findings.append(
                        SkillInspectionFinding(
                            rule_id=rule.rule_id,
                            category=rule.category,
                            severity=rule.severity,
                            logical_path=logical_path,
                            line=prefix.count("\n") + 1,
                            summary=rule.summary,
                            evidence_fingerprint=f"sha256:{evidence}",
                        )
                    )
        observed_fingerprint = f"sha256:{digest.hexdigest()}"
        if observed_fingerprint != expected_fingerprint:
            raise MishkanError(
                ErrorCode.SKILL_TRUST,
                "skill package differs from its provenance lock",
                details={"expected": expected_fingerprint, "observed": observed_fingerprint},
            )
        threshold = _SEVERITY_ORDER[self.profile.quarantine_threshold]
        quarantined = any(_SEVERITY_ORDER[item.severity] >= threshold for item in findings)
        return SkillInspectionResult(
            profile_id=self.profile.profile_id,
            profile_revision=self.profile.revision,
            profile_fingerprint=self.profile.fingerprint,
            package_fingerprint=observed_fingerprint,
            findings=tuple(findings),
            scanned_files=len(entries),
            scanned_bytes=scanned_bytes,
            quarantined=quarantined,
        )

    def _bounded_error(self, dimension: str) -> MishkanError:
        return MishkanError(
            ErrorCode.SKILL_TRUST,
            "skill package exceeds configured inspection bounds",
            details={"profile_id": self.profile.profile_id, "dimension": dimension},
        )


class SkillInspectionProfileLoader:
    def load(self, uri: str, project_root: Path) -> SkillInspectionProfile:
        try:
            raw: Any = yaml.safe_load(self._read(uri, project_root))
        except yaml.YAMLError as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "skill inspection profile is malformed YAML",
                details={"source": uri},
            ) from exc
        if not isinstance(raw, dict):
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "skill inspection profile must contain a mapping",
                details={"source": uri},
            )
        SchemaRegistry.require_supported("mishkan.skill", raw.get("schema_version"))
        try:
            return SkillInspectionProfile.model_validate(raw)
        except ValidationError as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "skill inspection profile is invalid",
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
                    "package skill-inspection URI must identify a module and resource",
                )
            return files(module).joinpath(resource).read_bytes()
        path = resolve_source_path(uri, project_root, "skill inspection profile")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "skill inspection profile cannot be read",
                details={"source": uri, "reason": type(exc).__name__},
            ) from exc
