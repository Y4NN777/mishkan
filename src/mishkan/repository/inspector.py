"""Read-only Git binding and configurable repository discovery."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.repository.models import DiscoveryFact, DiscoverySnapshot, RepositoryBinding
from mishkan.repository.profile import DiscoveryProfile, load_discovery_profile


class RepositoryInspector:
    def __init__(self, profile: DiscoveryProfile | None = None) -> None:
        self._profile = profile or load_discovery_profile()

    def bind(self, requested_path: Path) -> RepositoryBinding:
        path = requested_path.expanduser().resolve()
        root = Path(self._git_text(path, "rev-parse", "--show-toplevel")).resolve()
        revision = self._git_text(root, "rev-parse", "HEAD")
        remote = self._git_text(root, "remote", "get-url", "origin", required=False) or None
        identity_source = remote or str(root)
        return RepositoryBinding(
            repository_id=hashlib.sha256(identity_source.encode()).hexdigest(),
            root=root,
            base_revision=revision,
            remote_url=remote,
        )

    def discover(self, binding: RepositoryBinding) -> DiscoverySnapshot:
        files = self._tracked_files(binding.root)
        facts: list[DiscoveryFact] = []

        for kind, candidates in sorted(self._profile.manifests.items()):
            matches = tuple(path for path in files if path.name in candidates)
            if matches:
                facts.append(DiscoveryFact(kind=kind, value=matches[0].name, citations=matches))

        language_paths: dict[str, list[Path]] = {}
        for path in files:
            language = self._profile.languages.get(path.suffix.lower())
            if language:
                language_paths.setdefault(language, []).append(path)
        for language, paths in sorted(language_paths.items()):
            facts.append(
                DiscoveryFact(
                    kind="language",
                    value=language,
                    citations=tuple(paths[: self._profile.evidence_limit]),
                )
            )

        unknowns = []
        if not any(fact.kind == "readme" for fact in facts):
            unknowns.append("project documentation")
        if not any(fact.kind == "tests" for fact in facts):
            unknowns.append("test framework")

        canonical = {
            "repository_id": binding.repository_id,
            "base_revision": binding.base_revision,
            "facts": [fact.model_dump(mode="json") for fact in facts],
            "unknowns": unknowns,
        }
        fingerprint = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return DiscoverySnapshot(
            binding=binding,
            facts=tuple(facts),
            unknowns=tuple(unknowns),
            fingerprint=fingerprint,
        )

    def inspect(self, requested_path: Path) -> DiscoverySnapshot:
        return self.discover(self.bind(requested_path))

    def _tracked_files(self, root: Path) -> tuple[Path, ...]:
        entries = self._git_bytes(root, "ls-files", "-z").split(b"\0")
        excluded = frozenset(self._profile.excluded_directories)
        paths = []
        for entry in entries:
            if not entry:
                continue
            relative = Path(entry.decode("utf-8", errors="strict"))
            if not excluded.intersection(relative.parts):
                paths.append(relative)
        return tuple(sorted(paths))

    @staticmethod
    def _git_text(
        cwd: Path,
        *arguments: str,
        required: bool = True,
    ) -> str:
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=cwd,
                check=required,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise MishkanError(
                ErrorCode.PROJECT,
                "repository cannot be bound to a Git revision",
                details={"path": str(cwd), "operation": "git " + " ".join(arguments)},
            ) from exc
        if not required and completed.returncode != 0:
            return ""
        return str(completed.stdout).strip()

    @classmethod
    def _git_bytes(cls, cwd: Path, *arguments: str) -> bytes:
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=cwd,
                check=True,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise MishkanError(
                ErrorCode.PROJECT,
                "repository discovery failed",
                details={"path": str(cwd), "operation": "git " + " ".join(arguments)},
            ) from exc
        return completed.stdout
