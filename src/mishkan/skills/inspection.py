"""Bounded, configured inspection for immutable skill package candidates."""

from __future__ import annotations

import fnmatch
import hashlib
import re
from pathlib import Path, PurePosixPath

from mishkan.domain.errors import ErrorCode, MishkanError
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
        findings: list[SkillInspectionFinding] = []
        digest = hashlib.sha256()
        scanned_files = 0
        scanned_bytes = 0
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink():
                raise MishkanError(
                    ErrorCode.SKILL_CONTRACT,
                    "skill package inspection refuses symbolic links",
                )
            if not path.is_file():
                continue
            scanned_files += 1
            if scanned_files > self.profile.max_scanned_files:
                raise self._bounded_error("file count")
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
            scanned_files=scanned_files,
            scanned_bytes=scanned_bytes,
            quarantined=quarantined,
        )

    def _bounded_error(self, dimension: str) -> MishkanError:
        return MishkanError(
            ErrorCode.SKILL_TRUST,
            "skill package exceeds configured inspection bounds",
            details={"profile_id": self.profile.profile_id, "dimension": dimension},
        )
