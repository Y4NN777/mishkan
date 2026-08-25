"""Typed capability adapter ports and native filesystem implementations."""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import platform
import stat
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Protocol

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.tools.gateway_models import AdapterResult, ResolvedTargets
from mishkan.tools.isolation import ContainerCommand


@dataclass(frozen=True, slots=True)
class AdapterCall:
    arguments: dict[str, Any]
    targets: ResolvedTargets
    credentials: dict[str, str]


class CapabilityAdapter(Protocol):
    def invoke(self, call: AdapterCall) -> AdapterResult: ...


def _object_type(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "special"


def _object_identity(value: Any) -> dict[str, int]:
    return {"device": int(value.st_dev), "inode": int(value.st_ino)}


class FileResolveAdapter:
    adapter_id = "native.file.resolve"

    def invoke(self, call: AdapterCall) -> AdapterResult:
        target = call.targets.paths[0]
        exists = target.absolute.exists()
        object_type = _object_type(target.absolute.stat().st_mode) if exists else "missing"
        return AdapterResult(
            output={
                "requested_path": target.requested,
                "lexical_path": target.lexical_relative,
                "resolved_path": target.relative,
                "exists": exists,
                "object_type": object_type,
                "is_symlink": bool(target.link_chain),
                "link_chain": list(target.link_chain),
            },
            actual_targets=call.targets,
            evidence={"resolver": "pathlib.resolve"},
        )


class FileStatAdapter:
    adapter_id = "native.file.stat"

    def __init__(self, max_digest_bytes: int) -> None:
        self._max_digest_bytes = max_digest_bytes

    def invoke(self, call: AdapterCall) -> AdapterResult:
        target = call.targets.paths[0]
        try:
            observed = target.absolute.stat()
        except FileNotFoundError as exc:
            raise MishkanError(
                ErrorCode.FILE,
                "filesystem object was not found",
                details={"category": "not_found", "path": target.relative},
            ) from exc
        kind = _object_type(observed.st_mode)
        if kind == "special":
            raise MishkanError(
                ErrorCode.FILE,
                "filesystem object type is unsupported",
                details={"category": "unsupported_type", "path": target.relative},
            )
        digest: str | None = None
        if bool(call.arguments.get("digest", False)):
            if kind != "file":
                raise MishkanError(
                    ErrorCode.FILE,
                    "digest requires a regular file",
                    details={"category": "unsupported_type", "path": target.relative},
                )
            if observed.st_size > self._max_digest_bytes:
                raise MishkanError(
                    ErrorCode.FILE,
                    "file exceeds the configured digest limit",
                    details={
                        "category": "size_limit",
                        "path": target.relative,
                        "limit": self._max_digest_bytes,
                    },
                )
            digest = f"sha256:{hashlib.sha256(target.absolute.read_bytes()).hexdigest()}"
        return AdapterResult(
            output={
                "path": target.relative,
                "object_type": kind,
                "size": observed.st_size,
                "permissions": stat.S_IMODE(observed.st_mode),
                "modified_ns": observed.st_mtime_ns,
                "object_identity": _object_identity(observed),
                "digest": digest,
            },
            actual_targets=call.targets,
            evidence={"followed_target": True},
        )


class FileReadAdapter:
    adapter_id = "native.file.read"

    def __init__(self, max_bytes: int, max_scan_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._max_scan_bytes = max_scan_bytes

    def invoke(self, call: AdapterCall) -> AdapterResult:
        target = call.targets.paths[0]
        try:
            before = target.absolute.stat()
        except FileNotFoundError as exc:
            raise MishkanError(
                ErrorCode.FILE,
                "file was not found",
                details={"category": "not_found", "path": target.relative},
            ) from exc
        if not stat.S_ISREG(before.st_mode):
            raise MishkanError(
                ErrorCode.FILE,
                "read requires a regular file",
                details={"category": "unsupported_type", "path": target.relative},
            )
        requested_limit = int(call.arguments.get("max_bytes", self._max_bytes))
        if requested_limit < 1 or requested_limit > self._max_bytes:
            raise MishkanError(
                ErrorCode.FILE,
                "requested read bound exceeds the configured limit",
                details={"category": "size_limit", "limit": self._max_bytes},
            )
        mode = str(call.arguments["mode"])
        encoding = str(call.arguments.get("encoding", "utf-8"))
        binary_policy = str(call.arguments.get("binary_policy", "reject"))
        line_range: list[int] | None = None
        if mode in {"lines", "head", "tail"}:
            raw, byte_start, byte_end, line_range, truncated = self._read_lines(
                target.absolute,
                mode,
                call.arguments,
                requested_limit,
                encoding,
            )
        else:
            raw, byte_start, byte_end, truncated = self._read_bytes(
                target.absolute,
                mode,
                call.arguments,
                requested_limit,
                before.st_size,
                encoding,
            )
        content, content_format, observed_encoding = self._encode_content(
            raw,
            mode,
            encoding,
            binary_policy,
        )
        after = target.absolute.stat()
        changed = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        continuation = byte_end if truncated and mode not in {"lines", "head", "tail"} else None
        return AdapterResult(
            output={
                "path": target.relative,
                "mode": mode,
                "content": content,
                "content_format": content_format,
                "encoding": observed_encoding,
                "byte_range": [byte_start, byte_end],
                "line_range": line_range,
                "total_bytes": before.st_size,
                "content_digest": f"sha256:{hashlib.sha256(raw).hexdigest()}",
                "object_identity": _object_identity(before),
                "truncated": truncated,
                "continuation_offset": continuation,
                "changed_during_read": changed,
            },
            actual_targets=call.targets,
            evidence={"consistency": "stat_before_after"},
        )

    @staticmethod
    def _read_bytes(
        path: Path,
        mode: str,
        arguments: dict[str, Any],
        limit: int,
        total: int,
        encoding: str,
    ) -> tuple[bytes, int, int, bool]:
        if mode not in {"bytes", "text", "range"}:
            raise MishkanError(
                ErrorCode.FILE,
                "read mode is unsupported",
                details={"category": "invalid_query", "mode": mode},
            )
        offset = int(arguments.get("offset", 0))
        if offset < 0:
            raise MishkanError(ErrorCode.FILE, "read offset must not be negative")
        if offset > total:
            raise MishkanError(
                ErrorCode.FILE,
                "read offset is beyond the end of the file",
                details={"category": "invalid_query", "offset": offset, "total_bytes": total},
            )
        length_value = arguments.get("length")
        length = min(limit, int(length_value)) if length_value is not None else limit
        if length < 1:
            raise MishkanError(ErrorCode.FILE, "read length must be positive")
        with path.open("rb") as stream:
            stream.seek(offset)
            raw = stream.read(length)
        if mode in {"text", "range"}:
            bounded = raw
            raw = FileReadAdapter._trim_incomplete_character(raw, encoding)
            if bounded and not raw:
                raise MishkanError(
                    ErrorCode.FILE,
                    "read bound is too small for one encoded character",
                    details={"category": "size_limit", "limit": limit},
                )
        end = offset + len(raw)
        return raw, offset, end, end < total

    def _read_lines(
        self,
        path: Path,
        mode: str,
        arguments: dict[str, Any],
        limit: int,
        encoding: str,
    ) -> tuple[bytes, int, int, list[int] | None, bool]:
        size = path.stat().st_size
        if size > self._max_scan_bytes:
            raise MishkanError(
                ErrorCode.FILE,
                "line read exceeds the configured scan limit",
                details={"category": "size_limit", "limit": self._max_scan_bytes},
            )
        whole = path.read_bytes()
        try:
            lines = whole.decode(encoding, errors="strict").splitlines(keepends=True)
        except (LookupError, UnicodeDecodeError) as exc:
            raise MishkanError(
                ErrorCode.FILE,
                "file cannot be decoded with the requested encoding",
                details={"category": "encoding_error", "encoding": encoding},
            ) from exc
        if not lines:
            return b"", 0, 0, None, False
        if mode == "lines":
            start = int(arguments.get("start_line", 1))
            end = int(arguments.get("end_line", start))
        else:
            count = int(arguments.get("line_count", 20))
            if count < 1:
                raise MishkanError(ErrorCode.FILE, "line count must be positive")
            if mode == "head":
                start, end = 1, min(count, len(lines))
            else:
                start, end = max(1, len(lines) - count + 1), len(lines)
        if start < 1 or end < start:
            raise MishkanError(
                ErrorCode.FILE,
                "line range is invalid",
                details={"category": "invalid_query"},
            )
        if start > len(lines):
            raise MishkanError(
                ErrorCode.FILE,
                "requested line starts beyond the end of the file",
                details={
                    "category": "invalid_query",
                    "start_line": start,
                    "total_lines": len(lines),
                },
            )
        selected = "".join(lines[start - 1 : end]).encode(encoding)
        truncated = len(selected) > limit
        raw = self._trim_incomplete_character(selected[:limit], encoding)
        byte_start = len("".join(lines[: start - 1]).encode(encoding))
        return raw, byte_start, byte_start + len(raw), [start, min(end, len(lines))], truncated

    @staticmethod
    def _trim_incomplete_character(raw: bytes, encoding: str) -> bytes:
        try:
            raw.decode(encoding, errors="strict")
        except UnicodeDecodeError as exc:
            if exc.end == len(raw) and exc.start > 0:
                return raw[: exc.start]
        except LookupError:
            return raw
        return raw

    @staticmethod
    def _encode_content(
        raw: bytes,
        mode: str,
        encoding: str,
        binary_policy: str,
    ) -> tuple[str, str, str | None]:
        if mode == "bytes" or (b"\x00" in raw and binary_policy == "base64"):
            return base64.b64encode(raw).decode("ascii"), "base64", None
        if b"\x00" in raw:
            raise MishkanError(
                ErrorCode.FILE,
                "binary content is refused by the selected policy",
                details={"category": "binary_content"},
            )
        try:
            return raw.decode(encoding, errors="strict"), "text", encoding
        except (LookupError, UnicodeDecodeError) as exc:
            raise MishkanError(
                ErrorCode.FILE,
                "file cannot be decoded with the requested encoding",
                details={"category": "encoding_error", "encoding": encoding},
            ) from exc


class FileListAdapter:
    adapter_id = "native.file.list"

    def __init__(self, max_results: int, max_traversal_entries: int) -> None:
        self._max_results = max_results
        self._max_traversal_entries = max_traversal_entries

    def invoke(self, call: AdapterCall) -> AdapterResult:
        target = call.targets.paths[0]
        root = target.absolute
        try:
            before = root.stat()
        except FileNotFoundError as exc:
            raise MishkanError(
                ErrorCode.FILE,
                "listing root was not found",
                details={"category": "not_found", "path": target.relative},
            ) from exc
        if not stat.S_ISDIR(before.st_mode):
            raise MishkanError(
                ErrorCode.FILE,
                "listing requires a directory",
                details={"category": "unsupported_type", "path": target.relative},
            )
        if bool(call.arguments.get("follow_links", False)):
            raise MishkanError(
                ErrorCode.FILE,
                "link-following listing is not available in this adapter",
                details={"category": "unsupported_type"},
            )
        limit = int(call.arguments.get("max_results", self._max_results))
        if limit < 1 or limit > self._max_results:
            raise MishkanError(
                ErrorCode.FILE,
                "requested listing bound exceeds the configured limit",
                details={"category": "result_limit", "limit": self._max_results},
            )
        recursive = bool(call.arguments.get("recursive", False))
        max_depth = int(call.arguments.get("max_depth", 1 if not recursive else 32))
        include = tuple(str(item) for item in call.arguments.get("include", ("*",)))
        exclude = tuple(str(item) for item in call.arguments.get("exclude", ()))
        include_hidden = bool(call.arguments.get("include_hidden", False))
        object_types = frozenset(str(item) for item in call.arguments.get("object_types", ()))
        query_digest = self._query_digest(call.arguments)
        offset, expected_view_digest = self._cursor_state(
            call.arguments.get("cursor"), query_digest
        )
        entries, inaccessible, traversal_limited = self._walk(
            root,
            recursive=recursive,
            max_depth=max_depth,
            include=include,
            exclude=exclude,
            include_hidden=include_hidden,
            object_types=object_types,
        )
        entries.sort(key=lambda item: str(item["path"]).encode())
        view_digest = hashlib.sha256(
            json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
        if expected_view_digest is not None and expected_view_digest != view_digest:
            raise MishkanError(
                ErrorCode.FILE,
                "listed view changed since the continuation cursor was issued",
                details={"category": "changed_during_read"},
            )
        if offset > len(entries):
            raise MishkanError(
                ErrorCode.FILE,
                "listing cursor is beyond the available result set",
                details={"category": "invalid_query"},
            )
        page = entries[offset : offset + limit]
        next_offset = offset + len(page)
        has_more = next_offset < len(entries)
        truncated = has_more or traversal_limited
        after = root.stat()
        changed = (before.st_dev, before.st_ino, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_mtime_ns,
        )
        return AdapterResult(
            output={
                "root": target.relative,
                "entries": page,
                "ordering": "path-bytewise-ascending",
                "query_digest": query_digest,
                "view_digest": view_digest,
                "engine": "python.scandir",
                "engine_version": platform.python_version(),
                "ignore_evidence": {
                    "hidden": "included" if include_hidden else "excluded",
                    "include": list(include),
                    "exclude": list(exclude),
                },
                "inaccessible": inaccessible,
                "cycles": [],
                "truncated": truncated,
                "continuation_cursor": (
                    f"{query_digest}:{view_digest}:{next_offset}" if has_more else None
                ),
                "changed_during_list": changed,
            },
            actual_targets=call.targets,
            evidence={"traversal_entries_limit": self._max_traversal_entries},
        )

    @staticmethod
    def _query_digest(arguments: dict[str, Any]) -> str:
        normalized = {key: value for key, value in arguments.items() if key != "cursor"}
        payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @staticmethod
    def _cursor_state(cursor: Any, query_digest: str) -> tuple[int, str | None]:
        if cursor is None:
            return 0, None
        try:
            digest, view_digest, raw_offset = str(cursor).split(":", maxsplit=2)
            offset = int(raw_offset)
        except (TypeError, ValueError) as exc:
            raise MishkanError(
                ErrorCode.FILE,
                "listing cursor is invalid",
                details={"category": "invalid_query"},
            ) from exc
        if digest != query_digest or len(view_digest) != 16 or offset < 0:
            raise MishkanError(
                ErrorCode.FILE,
                "listing cursor does not match the normalized query",
                details={"category": "invalid_query"},
            )
        return offset, view_digest

    def _walk(
        self,
        root: Path,
        *,
        recursive: bool,
        max_depth: int,
        include: tuple[str, ...],
        exclude: tuple[str, ...],
        include_hidden: bool,
        object_types: frozenset[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]], bool]:
        entries: list[dict[str, Any]] = []
        inaccessible: list[dict[str, str]] = []
        pending: list[tuple[Path, int]] = [(root, 0)]
        visited = 0
        traversal_limited = False
        while pending and not traversal_limited:
            directory, depth = pending.pop()
            try:
                children = sorted(os.scandir(directory), key=lambda item: os.fsencode(item.name))
            except OSError as exc:
                relative = directory.relative_to(root).as_posix() or "."
                inaccessible.append({"path": relative, "error": type(exc).__name__})
                continue
            for child in children:
                visited += 1
                if visited > self._max_traversal_entries:
                    traversal_limited = True
                    break
                path = Path(child.path)
                relative = path.relative_to(root).as_posix()
                if not include_hidden and any(
                    part.startswith(".") for part in Path(relative).parts
                ):
                    continue
                if any(fnmatchcase(relative, pattern) for pattern in exclude):
                    continue
                try:
                    observed = child.stat(follow_symlinks=False)
                except OSError as exc:
                    inaccessible.append({"path": relative, "error": type(exc).__name__})
                    continue
                kind = _object_type(observed.st_mode)
                entry_depth = depth + 1
                if recursive and kind == "directory" and entry_depth < max_depth:
                    pending.append((path, entry_depth))
                if not any(fnmatchcase(relative, pattern) for pattern in include):
                    continue
                if object_types and kind not in object_types:
                    continue
                try:
                    link_target = os.readlink(path) if kind == "symlink" else None
                except OSError as exc:
                    inaccessible.append({"path": relative, "error": type(exc).__name__})
                    continue
                entries.append(
                    {
                        "path": relative,
                        "object_type": kind,
                        "depth": entry_depth,
                        "size": observed.st_size if kind == "file" else None,
                        "link_target": link_target,
                    }
                )
        return entries, inaccessible, traversal_limited


class ReadFileAdapter:
    adapter_id = "native.repository.read_file"

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes

    def invoke(self, call: AdapterCall) -> AdapterResult:
        target = call.targets.paths[0]
        content = target.absolute.read_bytes()
        if len(content) > self._max_bytes:
            raise ValueError("resolved file exceeds the configured tool contract limit")
        return AdapterResult(
            output={"path": target.relative, "content": content.decode(errors="replace")},
            actual_targets=call.targets,
            evidence={"bytes_read": len(content)},
        )


class WriteFileAdapter:
    adapter_id = "native.repository.write_file"

    def invoke(self, call: AdapterCall) -> AdapterResult:
        target = call.targets.paths[0]
        content = str(call.arguments["content"])
        prior = target.absolute.read_text(encoding="utf-8") if target.absolute.exists() else ""
        target.absolute.parent.mkdir(parents=True, exist_ok=True)
        target.absolute.write_text(content, encoding="utf-8")
        diff = "".join(
            difflib.unified_diff(
                prior.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{target.relative}",
                tofile=f"b/{target.relative}",
            )
        )
        return AdapterResult(
            output={"path": target.relative, "bytes_written": len(content.encode()), "diff": diff},
            actual_targets=call.targets,
            evidence={"changed": prior != content},
        )


class ContainerCommandAdapter:
    adapter_id = "isolation.command"

    def __init__(self, command: ContainerCommand) -> None:
        self._command = command

    def invoke(self, call: AdapterCall) -> AdapterResult:
        workspace = call.targets.paths[0].absolute
        argv_value = call.arguments["argv"]
        if not isinstance(argv_value, list) or not all(
            isinstance(item, str) for item in argv_value
        ):
            raise ValueError("command argv must contain only strings")
        try:
            completed = self._command.run(workspace, tuple(argv_value))
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("isolated command exceeded its configured timeout") from exc
        return AdapterResult(
            output={
                "exit_code": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            },
            actual_targets=call.targets,
            evidence={"runtime": self._command.profile.runtime.value},
        )


class StatefulBackend(Protocol):
    def invoke(self, capability: str, call: AdapterCall) -> AdapterResult: ...


class _StatefulAdapter:
    capability: str

    def __init__(self, backend: StatefulBackend) -> None:
        self._backend = backend

    def invoke(self, call: AdapterCall) -> AdapterResult:
        return self._backend.invoke(self.capability, call)


class GitCommitAdapter(_StatefulAdapter):
    capability = "git.commit"


class GitPushAdapter(_StatefulAdapter):
    capability = "git.push"


class DeploymentAdapter(_StatefulAdapter):
    capability = "deployment.apply"


class ReleaseAdapter(_StatefulAdapter):
    capability = "release.publish"


class MigrationAdapter(_StatefulAdapter):
    capability = "migration.apply"
