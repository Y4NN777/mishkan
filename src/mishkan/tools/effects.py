"""Bounded worktree snapshots for truthful command-driven mutation evidence."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    fingerprint: str
    entries: dict[str, dict[str, Any]]
    complete: bool
    omissions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceEffectEvidence:
    base_fingerprint: str
    after_fingerprint: str
    changed_paths: tuple[str, ...]
    scope_deviations: tuple[str, ...]
    complete: bool
    omissions: tuple[str, ...]
    diff: bytes


class WorkspaceEffectObserver:
    """Observe filesystem effects without following links or executing repository content."""

    def snapshot(self, root: Path, configuration: dict[str, Any]) -> WorkspaceSnapshot:
        max_entries = int(configuration["max_entries"])
        max_file_bytes = int(configuration["max_file_bytes"])
        max_total_bytes = int(configuration["max_total_bytes"])
        exclusions = tuple(str(item) for item in configuration.get("exclude", ()))
        entries: dict[str, dict[str, Any]] = {}
        omissions: list[str] = []
        total_bytes = 0
        complete = True
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                children = sorted(os.scandir(directory), key=lambda item: os.fsencode(item.name))
            except OSError as exc:
                complete = False
                omissions.append(
                    f"scan:{directory.relative_to(root).as_posix()}:{type(exc).__name__}"
                )
                continue
            for child in children:
                path = Path(child.path)
                relative = path.relative_to(root).as_posix()
                if any(fnmatchcase(relative, pattern) for pattern in exclusions):
                    continue
                if len(entries) >= max_entries:
                    omissions.append("entry_limit_reached")
                    complete = False
                    pending.clear()
                    break
                try:
                    metadata = child.stat(follow_symlinks=False)
                except OSError as exc:
                    complete = False
                    omissions.append(f"stat:{relative}:{type(exc).__name__}")
                    continue
                mode = metadata.st_mode
                if stat.S_ISDIR(mode):
                    entries[relative] = {
                        "kind": "directory",
                        "mode": stat.S_IMODE(mode),
                    }
                    pending.append(path)
                    continue
                if stat.S_ISLNK(mode):
                    try:
                        target = os.readlink(path)
                    except OSError as exc:
                        complete = False
                        omissions.append(f"link:{relative}:{type(exc).__name__}")
                        continue
                    entries[relative] = {
                        "kind": "symlink",
                        "mode": stat.S_IMODE(mode),
                        "target": target,
                    }
                    continue
                if not stat.S_ISREG(mode):
                    entries[relative] = {
                        "kind": "special",
                        "mode": stat.S_IMODE(mode),
                    }
                    omissions.append(f"special:{relative}")
                    complete = False
                    continue
                if (
                    metadata.st_size > max_file_bytes
                    or total_bytes + metadata.st_size > max_total_bytes
                ):
                    omissions.append(f"content_limit:{relative}")
                    complete = False
                    entries[relative] = {
                        "kind": "file-unexamined",
                        "mode": stat.S_IMODE(mode),
                        "size": metadata.st_size,
                    }
                    continue
                try:
                    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(descriptor, "rb") as stream:
                        content = stream.read(max_file_bytes + 1)
                        observed = os.fstat(stream.fileno())
                except OSError as exc:
                    complete = False
                    omissions.append(f"read:{relative}:{type(exc).__name__}")
                    continue
                if (
                    observed.st_dev,
                    observed.st_ino,
                    observed.st_size,
                    observed.st_mtime_ns,
                ) != (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                ):
                    complete = False
                    omissions.append(f"changed_during_snapshot:{relative}")
                    continue
                total_bytes += len(content)
                entries[relative] = {
                    "kind": "file",
                    "mode": stat.S_IMODE(mode),
                    "size": len(content),
                    "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
                }
        fingerprint = hashlib.sha256(
            json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return WorkspaceSnapshot(fingerprint, entries, complete, tuple(omissions))

    @staticmethod
    def compare(
        before: WorkspaceSnapshot,
        after: WorkspaceSnapshot,
        *,
        allowed_scopes: tuple[str, ...],
    ) -> WorkspaceEffectEvidence:
        changed = tuple(
            sorted(
                path
                for path in set(before.entries) | set(after.entries)
                if before.entries.get(path) != after.entries.get(path)
            )
        )
        deviations = tuple(
            path
            for path in changed
            if not any(
                scope in {"", "."} or path == scope or Path(path).is_relative_to(Path(scope))
                for scope in allowed_scopes
            )
        )
        records = [
            {
                "path": path,
                "before": before.entries.get(path),
                "after": after.entries.get(path),
            }
            for path in changed
        ]
        diff = (json.dumps(records, sort_keys=True, indent=2) + "\n").encode()
        return WorkspaceEffectEvidence(
            base_fingerprint=before.fingerprint,
            after_fingerprint=after.fingerprint,
            changed_paths=changed,
            scope_deviations=deviations,
            complete=before.complete and after.complete,
            omissions=tuple(dict.fromkeys((*before.omissions, *after.omissions))),
            diff=diff,
        )
