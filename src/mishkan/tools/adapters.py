"""Typed capability adapter ports and native filesystem implementations."""

from __future__ import annotations

import ast
import base64
import difflib
import hashlib
import json
import os
import platform
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, ClassVar, Protocol
from uuid import UUID

from pydantic import ValidationError

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.policy.models import ResourceRequest
from mishkan.tools.execution import (
    EffectSettlement,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    ShellProfile,
)
from mishkan.tools.gateway_models import (
    AdapterResult,
    ArtifactCandidate,
    CallStatus,
    ResolvedTargets,
)
from mishkan.tools.isolation import ContainerCommand


@dataclass(frozen=True, slots=True)
class AdapterCall:
    arguments: dict[str, Any]
    targets: ResolvedTargets
    credentials: dict[str, str]
    execution_id: str
    resources: ResourceRequest
    isolation_profile: str | None
    cancellation_requested: Callable[[], bool]
    run_id: str
    task_attempt_id: str
    acting_identity: str
    capability: str
    plan_fingerprint: str = ""
    objective_class: str = "capability-invocation"
    repository: str = ""
    outcome: str = "capability-invocation"
    role: str = "agent"


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


class SearchFilesAdapter:
    adapter_id = "native.search.files"

    def __init__(self, max_results: int, max_traversal_entries: int) -> None:
        self._max_results = max_results
        self._listing = FileListAdapter(max_results, max_traversal_entries)

    def invoke(self, call: AdapterCall) -> AdapterResult:
        translated = {
            "path": call.arguments["path"],
            "recursive": True,
            "max_depth": call.arguments.get("max_depth", 32),
            "include": call.arguments.get("patterns", ["*"]),
            "exclude": call.arguments.get("exclude", []),
            "include_hidden": call.arguments.get("include_hidden", False),
            "follow_links": False,
            "object_types": call.arguments.get("object_types", ["file"]),
            "max_results": call.arguments.get("max_results", self._max_results),
        }
        if "cursor" in call.arguments:
            translated["cursor"] = call.arguments["cursor"]
        listed = self._listing.invoke(replace(call, arguments=translated))
        output = listed.output
        return AdapterResult(
            output={
                "root": output["root"],
                "matches": output["entries"],
                "ordering": output["ordering"],
                "query_digest": output["query_digest"],
                "view_digest": output["view_digest"],
                "engine": output["engine"],
                "engine_version": output["engine_version"],
                "ignore_evidence": output["ignore_evidence"],
                "inaccessible": output["inaccessible"],
                "truncated": output["truncated"],
                "continuation_cursor": output["continuation_cursor"],
                "changed_during_search": output["changed_during_list"],
            },
            actual_targets=call.targets,
            evidence={"selection": "bounded native traversal"},
        )


def _bounded_python_sources(
    root: Path,
    *,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    include_hidden: bool,
    max_files: int,
) -> tuple[list[Path], list[str], bool]:
    sources: list[Path] = []
    omissions: list[str] = []
    truncated = False
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(
            name
            for name in names
            if not (Path(directory) / name).is_symlink()
            and (include_hidden or not name.startswith("."))
        )
        for name in sorted(files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_file():
                omissions.append(f"unsupported_object:{relative}")
                continue
            if not include_hidden and any(part.startswith(".") for part in Path(relative).parts):
                continue
            if not any(fnmatchcase(relative, pattern) for pattern in include):
                continue
            if any(fnmatchcase(relative, pattern) for pattern in exclude):
                continue
            if len(sources) >= max_files:
                truncated = True
                omissions.append("file_limit_reached")
                return sources, omissions, truncated
            sources.append(path)
    return sources, omissions, truncated


def _python_node_name(node: ast.AST) -> str | None:
    value = getattr(node, "name", None)
    if isinstance(value, str):
        return value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _python_node_name(node.func)
    return None


class PythonStructuralSearchAdapter:
    """Bounded Python syntax search that never claims semantic coverage."""

    adapter_id = "python.ast.search.structure"
    _NODE_TYPES: ClassVar[dict[str, tuple[type[ast.AST], ...]]] = {
        "FunctionDef": (ast.FunctionDef,),
        "AsyncFunctionDef": (ast.AsyncFunctionDef,),
        "ClassDef": (ast.ClassDef,),
        "Call": (ast.Call,),
        "Import": (ast.Import,),
        "ImportFrom": (ast.ImportFrom,),
        "Assign": (ast.Assign,),
        "AnnAssign": (ast.AnnAssign,),
    }

    def __init__(self, *, max_results: int, max_files: int, max_file_bytes: int) -> None:
        self._max_results = max_results
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes

    def invoke(self, call: AdapterCall) -> AdapterResult:
        target = call.targets.paths[0]
        if not target.absolute.is_dir():
            raise MishkanError(ErrorCode.FILE, "structural search root must be a directory")
        result_limit = min(
            int(call.arguments.get("max_results", self._max_results)), self._max_results
        )
        file_limit = min(int(call.arguments.get("max_files", self._max_files)), self._max_files)
        requested_types = tuple(str(value) for value in call.arguments["node_types"])
        node_types = tuple(
            node_type for requested in requested_types for node_type in self._NODE_TYPES[requested]
        )
        name = call.arguments.get("name")
        include = tuple(str(value) for value in call.arguments.get("include", ("*.py", "**/*.py")))
        exclude = tuple(str(value) for value in call.arguments.get("exclude", ()))
        sources, omissions, truncated = _bounded_python_sources(
            target.absolute,
            include=include,
            exclude=exclude,
            include_hidden=bool(call.arguments.get("include_hidden", False)),
            max_files=file_limit,
        )
        matches: list[dict[str, Any]] = []
        failures: list[str] = []
        examined_files = 0
        for path in sources:
            relative = path.relative_to(target.absolute).as_posix()
            size = path.stat().st_size
            if size > self._max_file_bytes:
                omissions.append(f"file_size_limit:{relative}")
                continue
            try:
                tree = ast.parse(path.read_bytes(), filename=relative)
            except (OSError, SyntaxError, UnicodeError) as exc:
                failures.append(f"{relative}:{type(exc).__name__}")
                continue
            examined_files += 1
            for node in ast.walk(tree):
                if not isinstance(node, node_types):
                    continue
                observed_name = _python_node_name(node)
                if name is not None and observed_name != name:
                    continue
                matches.append(
                    {
                        "path": relative,
                        "node_type": type(node).__name__,
                        "name": observed_name,
                        "line": int(getattr(node, "lineno", 1)),
                        "end_line": int(getattr(node, "end_lineno", getattr(node, "lineno", 1))),
                    }
                )
                if len(matches) >= result_limit:
                    truncated = True
                    omissions.append("result_limit_reached")
                    break
            if len(matches) >= result_limit:
                break
        normalized_query = {
            "language": "python",
            "node_types": sorted(requested_types),
            "name": name,
            "include": list(include),
            "exclude": list(exclude),
        }
        return AdapterResult(
            output={
                "root": target.relative,
                "matches": matches,
                "engine": "python.ast",
                "engine_version": sys.version.split()[0],
                "coverage": "syntax",
                "semantic_coverage": False,
                "normalized_query": normalized_query,
                "query_digest": hashlib.sha256(
                    json.dumps(normalized_query, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "freshness": "live-worktree",
                "limits": {
                    "max_results": result_limit,
                    "max_files": file_limit,
                    "max_file_bytes": self._max_file_bytes,
                },
                "examined_files": examined_files,
                "omissions": omissions,
                "failures": failures,
                "truncated": truncated,
                "continuation_cursor": None,
            },
            actual_targets=call.targets,
            evidence={"parser": "stdlib-ast", "semantic_resolution": False},
        )


class PythonSymbolSearchAdapter:
    """Search Python definitions and references with syntax-only attribution."""

    adapter_id = "python.ast.search.symbol"

    def __init__(self, *, max_results: int, max_files: int, max_file_bytes: int) -> None:
        self._max_results = max_results
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes

    def invoke(self, call: AdapterCall) -> AdapterResult:
        target = call.targets.paths[0]
        if not target.absolute.is_dir():
            raise MishkanError(ErrorCode.FILE, "symbol search root must be a directory")
        result_limit = min(
            int(call.arguments.get("max_results", self._max_results)), self._max_results
        )
        file_limit = min(int(call.arguments.get("max_files", self._max_files)), self._max_files)
        identifier = str(call.arguments["identifier"])
        relation = str(call.arguments.get("relation", "both"))
        include = tuple(str(value) for value in call.arguments.get("include", ("*.py", "**/*.py")))
        exclude = tuple(str(value) for value in call.arguments.get("exclude", ()))
        sources, omissions, truncated = _bounded_python_sources(
            target.absolute,
            include=include,
            exclude=exclude,
            include_hidden=bool(call.arguments.get("include_hidden", False)),
            max_files=file_limit,
        )
        matches: list[dict[str, Any]] = []
        failures: list[str] = []
        examined_files = 0
        for path in sources:
            relative = path.relative_to(target.absolute).as_posix()
            if path.stat().st_size > self._max_file_bytes:
                omissions.append(f"file_size_limit:{relative}")
                continue
            try:
                tree = ast.parse(path.read_bytes(), filename=relative)
            except (OSError, SyntaxError, UnicodeError) as exc:
                failures.append(f"{relative}:{type(exc).__name__}")
                continue
            examined_files += 1
            for node in ast.walk(tree):
                observed_relation: str | None = None
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and node.name == identifier
                ):
                    observed_relation = "definition"
                elif isinstance(node, ast.Name) and node.id == identifier:
                    observed_relation = (
                        "definition" if isinstance(node.ctx, ast.Store) else "reference"
                    )
                elif isinstance(node, ast.Attribute) and node.attr == identifier:
                    observed_relation = "reference"
                if observed_relation is None or relation not in {"both", observed_relation}:
                    continue
                matches.append(
                    {
                        "path": relative,
                        "relation": observed_relation,
                        "syntax_kind": type(node).__name__,
                        "line": int(getattr(node, "lineno", 1)),
                        "column": int(getattr(node, "col_offset", 0)),
                    }
                )
                if len(matches) >= result_limit:
                    truncated = True
                    omissions.append("result_limit_reached")
                    break
            if len(matches) >= result_limit:
                break
        normalized_query = {
            "language": "python",
            "identifier": identifier,
            "relation": relation,
            "include": list(include),
            "exclude": list(exclude),
        }
        return AdapterResult(
            output={
                "root": target.relative,
                "matches": matches,
                "engine": "python.ast",
                "engine_version": sys.version.split()[0],
                "coverage": "syntax",
                "semantic_coverage": False,
                "normalized_query": normalized_query,
                "query_digest": hashlib.sha256(
                    json.dumps(normalized_query, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "freshness": "live-worktree",
                "limits": {
                    "max_results": result_limit,
                    "max_files": file_limit,
                    "max_file_bytes": self._max_file_bytes,
                },
                "examined_files": examined_files,
                "omissions": omissions,
                "failures": failures,
                "truncated": truncated,
                "continuation_cursor": None,
            },
            actual_targets=call.targets,
            evidence={"parser": "stdlib-ast", "semantic_resolution": False},
        )


class GitHistorySearchAdapter:
    """Bounded local Git history search with no implicit network operation."""

    adapter_id = "git.search.history"

    def __init__(
        self, executable: Path, version: str, *, max_results: int, timeout_seconds: float
    ) -> None:
        self._executable = executable
        self._version = version
        self._max_results = max_results
        self._timeout_seconds = timeout_seconds

    def invoke(self, call: AdapterCall) -> AdapterResult:
        target = call.targets.paths[0]
        if not target.absolute.is_dir():
            raise MishkanError(ErrorCode.FILE, "history search root must be a directory")
        limit = min(int(call.arguments.get("max_results", self._max_results)), self._max_results)
        field = str(call.arguments.get("field", "message"))
        semantics = str(call.arguments.get("semantics", "literal"))
        query = str(call.arguments["query"])
        argv = [
            str(self._executable),
            "-c",
            "color.ui=false",
            "--no-pager",
            "log",
            "--format=%H%x1f%aI%x1f%an%x1f%s%x1e",
            f"--max-count={limit + 1}",
        ]
        if field == "message":
            if semantics == "literal":
                argv.append("--fixed-strings")
            argv.append(f"--grep={query}")
        elif semantics == "literal":
            argv.append(f"-S{query}")
        else:
            argv.append(f"-G{query}")
        argv.extend(("--", "."))
        try:
            completed = subprocess.run(
                argv,
                cwd=target.absolute,
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                env={"LC_ALL": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MishkanError(ErrorCode.FILE, "local Git history search failed") from exc
        failures = [line for line in completed.stderr.splitlines() if line]
        records: list[dict[str, str]] = []
        for raw_record in completed.stdout.split("\x1e"):
            fields = raw_record.strip().split("\x1f")
            if len(fields) != 4:
                continue
            commit, authored_at, author, subject = fields
            records.append(
                {
                    "commit": commit,
                    "authored_at": authored_at,
                    "author": author,
                    "subject": subject,
                }
            )
        truncated = len(records) > limit
        del records[limit:]
        normalized_query = {"field": field, "semantics": semantics, "query": query}
        return AdapterResult(
            output={
                "root": target.relative,
                "matches": records,
                "engine": "git",
                "engine_version": self._version,
                "coverage": "committed-history",
                "semantic_coverage": False,
                "normalized_query": normalized_query,
                "query_digest": hashlib.sha256(
                    json.dumps(normalized_query, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "freshness": "local-object-database",
                "limits": {"max_results": limit},
                "omissions": (["working-tree-not-searched"] if field == "content" else []),
                "failures": failures,
                "truncated": truncated,
                "continuation_cursor": None,
                "exit_code": completed.returncode,
            },
            actual_targets=call.targets,
            evidence={"executable": str(self._executable), "network": False},
            call_status=CallStatus.COMPLETED if completed.returncode == 0 else CallStatus.FAILED,
            error_code=None if completed.returncode == 0 else ErrorCode.FILE.value,
            reason=None if completed.returncode == 0 else "Git history engine returned failure",
        )


class RipgrepTextSearchAdapter:
    adapter_id = "ripgrep.search.text"

    def __init__(
        self,
        executable: Path,
        version: str,
        *,
        max_results: int,
        max_output_bytes: int,
        timeout_seconds: float,
    ) -> None:
        if not executable.is_absolute():
            raise ValueError("ripgrep executable must be an absolute path")
        self._executable = executable
        self._version = version
        self._max_results = max_results
        self._max_output_bytes = max_output_bytes
        self._timeout_seconds = timeout_seconds

    def invoke(self, call: AdapterCall) -> AdapterResult:
        target = call.targets.paths[0]
        if not target.absolute.is_dir():
            raise MishkanError(
                ErrorCode.FILE,
                "text search root must be a directory",
                details={"category": "unsupported_type", "path": target.relative},
            )
        result_limit = int(call.arguments.get("max_results", self._max_results))
        if result_limit < 1 or result_limit > self._max_results:
            raise MishkanError(
                ErrorCode.FILE,
                "requested text-search bound exceeds the configured limit",
                details={"category": "result_limit", "limit": self._max_results},
            )
        argv = self._argv(call.arguments)
        started = time.monotonic()
        process = subprocess.Popen(
            argv,
            cwd=target.absolute,
            env={"LC_ALL": "C.UTF-8", "RIPGREP_CONFIG_PATH": ""},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        matches: list[dict[str, Any]] = []
        failures: list[str] = []
        output_bytes = 0
        truncated = False
        partial_reason: str | None = None
        selector = selectors.DefaultSelector()
        assert process.stdout is not None
        assert process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            while selector.get_map():
                remaining = self._timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    truncated = True
                    partial_reason = "timeout"
                    self._stop(process)
                    break
                events = selector.select(min(remaining, 0.1))
                if not events and process.poll() is not None:
                    break
                for key, _ in events:
                    descriptor = (
                        key.fileobj if isinstance(key.fileobj, int) else key.fileobj.fileno()
                    )
                    chunk = os.read(descriptor, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output_bytes += len(chunk)
                    if output_bytes > self._max_output_bytes:
                        truncated = True
                        partial_reason = "output_limit"
                        self._stop(process)
                        break
                    channel = str(key.data)
                    buffers[channel].extend(chunk)
                    if channel == "stdout":
                        self._consume_json_lines(buffers[channel], matches)
                        if len(matches) >= result_limit:
                            del matches[result_limit:]
                            truncated = True
                            partial_reason = "result_limit"
                            self._stop(process)
                            break
                    else:
                        self._consume_error_lines(buffers[channel], failures)
                if truncated:
                    break
            if process.poll() is None:
                process.wait(timeout=1)
        finally:
            selector.close()
            if process.poll() is None:
                self._stop(process)
        self._consume_json_lines(buffers["stdout"], matches, final=True)
        self._consume_error_lines(buffers["stderr"], failures, final=True)
        if len(matches) > result_limit:
            del matches[result_limit:]
            truncated = True
            partial_reason = partial_reason or "result_limit"
        exit_code = process.returncode if process.returncode is not None else -1
        if exit_code not in {0, 1} and partial_reason is None:
            partial_reason = "engine_failure"
        query_digest = hashlib.sha256(
            json.dumps(call.arguments, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return AdapterResult(
            output={
                "root": target.relative,
                "matches": matches,
                "engine": "ripgrep",
                "engine_version": self._version,
                "query_digest": query_digest,
                "search_options": {
                    "semantics": str(call.arguments.get("semantics", "literal")),
                    "case": str(call.arguments.get("case", "smart")),
                    "include_hidden": bool(call.arguments.get("include_hidden", False)),
                    "include_ignored": bool(call.arguments.get("include_ignored", False)),
                    "binary_policy": str(call.arguments.get("binary_policy", "skip")),
                },
                "failures": failures,
                "truncated": truncated,
                "partial_reason": partial_reason,
                "continuation_cursor": None,
                "exit_code": exit_code,
            },
            actual_targets=call.targets,
            evidence={
                "executable": str(self._executable),
                "captured_bytes": output_bytes,
                "shell": False,
            },
        )

    def _argv(self, arguments: dict[str, Any]) -> list[str]:
        argv = [
            str(self._executable),
            "--json",
            "--no-config",
            "--color=never",
            "--sort=path",
        ]
        if str(arguments.get("semantics", "literal")) == "literal":
            argv.append("--fixed-strings")
        case = str(arguments.get("case", "smart"))
        argv.append(
            {
                "smart": "--smart-case",
                "sensitive": "--case-sensitive",
                "insensitive": "--ignore-case",
            }[case]
        )
        if bool(arguments.get("word", False)):
            argv.append("--word-regexp")
        if bool(arguments.get("multiline", False)):
            argv.extend(("--multiline", "--multiline-dotall"))
        if bool(arguments.get("include_hidden", False)):
            argv.append("--hidden")
        if bool(arguments.get("include_ignored", False)):
            argv.append("--no-ignore")
        if str(arguments.get("binary_policy", "skip")) == "text":
            argv.append("--text")
        for pattern in arguments.get("include", ()):
            argv.extend(("--glob", str(pattern)))
        for pattern in arguments.get("exclude", ()):
            argv.extend(("--glob", f"!{pattern}"))
        argv.extend(("--", str(arguments["query"]), "."))
        return argv

    @staticmethod
    def _consume_json_lines(
        buffer: bytearray,
        matches: list[dict[str, Any]],
        *,
        final: bool = False,
    ) -> None:
        while b"\n" in buffer or (final and buffer):
            raw, separator, remainder = buffer.partition(b"\n")
            if not separator and not final:
                break
            buffer[:] = remainder
            try:
                event = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if event.get("type") != "match":
                continue
            data = event["data"]
            matches.append(
                {
                    "path": data["path"].get("text"),
                    "line_number": data.get("line_number"),
                    "absolute_offset": data.get("absolute_offset"),
                    "line": data["lines"].get("text"),
                    "submatches": [
                        {
                            "text": item["match"].get("text"),
                            "start": item["start"],
                            "end": item["end"],
                        }
                        for item in data.get("submatches", ())
                    ],
                }
            )

    @staticmethod
    def _consume_error_lines(
        buffer: bytearray,
        failures: list[str],
        *,
        final: bool = False,
    ) -> None:
        while b"\n" in buffer or (final and buffer):
            raw, separator, remainder = buffer.partition(b"\n")
            if not separator and not final:
                break
            buffer[:] = remainder
            failures.append(raw.decode("utf-8", errors="replace"))

    @staticmethod
    def _stop(process: subprocess.Popen[bytes]) -> None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


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


class DirectProcessAdapter:
    """Execute one canonical absolute executable with a literal argv and no shell."""

    adapter_id = "native.process.exec"

    def __init__(
        self,
        *,
        max_output_bytes: int,
        max_stdin_bytes: int,
        max_environment_entries: int,
        execution_location: str = "local",
    ) -> None:
        if min(max_output_bytes, max_stdin_bytes, max_environment_entries) < 1:
            raise ValueError("direct process bounds must be positive")
        self._max_output_bytes = max_output_bytes
        self._max_stdin_bytes = max_stdin_bytes
        self._max_environment_entries = max_environment_entries
        self._execution_location = execution_location

    def invoke(self, call: AdapterCall) -> AdapterResult:
        request = self._request(call)
        self._validate_constraints(request, call)
        assert request.timeout_seconds is not None
        assert request.output_policy is not None
        stdin = request.stdin.encode("utf-8") if request.stdin is not None else b""
        executable = self._executable(request, call)
        cwd = self._cwd(call)
        environment = self._environment(request, call)
        started_at = utc_now()
        started_monotonic = time.monotonic()
        try:
            process = subprocess.Popen(
                (str(executable), *request.args),
                cwd=cwd,
                env=environment,
                stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise MishkanError(
                ErrorCode.EXECUTION,
                "direct process could not be spawned",
                details={"category": "spawn_failure", "reason": type(exc).__name__},
            ) from exc
        stdout, stderr, termination_cause = self._communicate(
            process,
            stdin,
            timeout_seconds=request.timeout_seconds,
            started_monotonic=started_monotonic,
            cancellation_requested=call.cancellation_requested,
        )
        return_code = process.returncode
        if return_code is None:
            raise MishkanError(
                ErrorCode.EXECUTION,
                "direct process did not settle after termination",
                details={"category": "lost_process"},
            )
        status = self._status(termination_cause, return_code, request.expected_exit_codes)
        effect_settlement = (
            EffectSettlement.ABSENT if not request.declared_effects else EffectSettlement.UNCERTAIN
        )
        outer_status = self._outer_status(status, termination_cause, request.declared_effects)
        result = ExecutionResult(
            execution_id=UUID(call.execution_id),
            mode=ExecutionMode.PROCESS,
            status=status,
            executable=str(executable),
            args=request.args,
            cwd=call.targets.paths[0].relative,
            exit_code=return_code if return_code >= 0 else None,
            signal=-return_code if return_code < 0 else None,
            started_at=started_at,
            finished_at=utc_now(),
            stdout_preview=self._preview(stdout, request.output_policy.preview_bytes),
            stderr_preview=self._preview(stderr, request.output_policy.preview_bytes),
            stdout_bytes=len(stdout),
            stderr_bytes=len(stderr),
            stdout_digest=f"sha256:{hashlib.sha256(stdout).hexdigest()}",
            stderr_digest=f"sha256:{hashlib.sha256(stderr).hexdigest()}",
            truncated=(
                len(stdout) > request.output_policy.preview_bytes
                or len(stderr) > request.output_policy.preview_bytes
                or termination_cause == "output_limit"
            ),
            termination_cause=termination_cause,
            expected_exit_codes=request.expected_exit_codes,
            environment_names=tuple(sorted(request.environment)),
            credential_environment_names=tuple(sorted(request.credential_environment)),
            declared_effects=request.declared_effects,
            effect_settlement=effect_settlement,
            execution_location=self._execution_location,
            error=self._error(status, termination_cause, return_code),
        )
        return AdapterResult(
            output=result.model_dump(mode="json"),
            actual_targets=call.targets,
            evidence={
                "adapter": self.adapter_id,
                "shell": False,
                "argv_count": len(request.args) + 1,
                "process_group": "isolated",
                "ambient_environment": False,
                "memory_limit_mb": call.resources.memory_mb,
                "network_denial_enforced": False,
            },
            inspection_content=(
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            ),
            artifact_candidates=self._artifact_candidates(
                request, stdout, stderr, termination_cause
            ),
            call_status=outer_status,
            retryable=False,
            error_code=(
                ErrorCode.EXECUTION.value if outer_status is not CallStatus.COMPLETED else None
            ),
            reason=f"direct process settled as {status.value}",
        )

    def _validate_constraints(self, request: ExecutionRequest, call: AdapterCall) -> None:
        if request.mode is not ExecutionMode.PROCESS:
            raise MishkanError(
                ErrorCode.EXECUTION,
                "direct process adapter received a non-process execution mode",
                details={"category": "invalid_mode"},
            )
        self._validate_common_constraints(request, call)

    def _validate_common_constraints(self, request: ExecutionRequest, call: AdapterCall) -> None:
        assert request.timeout_seconds is not None
        assert request.output_policy is not None
        if request.declared_effects:
            raise MishkanError(
                ErrorCode.TOOL_UNAVAILABLE,
                "native host execution cannot contain a state-changing command",
                details={
                    "constraint": "filesystem-scope",
                    "required_adapter": "isolated execution adapter",
                },
            )
        if not call.resources.network:
            raise MishkanError(
                ErrorCode.TOOL_UNAVAILABLE,
                "native execution adapter cannot enforce the requested network denial",
                details={"constraint": "network:false"},
            )
        if call.resources.memory_mb is not None:
            raise MishkanError(
                ErrorCode.TOOL_UNAVAILABLE,
                "native execution adapter cannot enforce the requested strict memory limit",
                details={
                    "constraint": "memory_mb",
                    "platform": platform.system(),
                    "required_adapter": "isolated execution adapter",
                },
            )
        if request.timeout_seconds > call.resources.timeout_seconds:
            raise MishkanError(
                ErrorCode.EXECUTION,
                "requested execution timeout exceeds the authorized resource bound",
                details={"category": "resource_limit"},
            )
        if request.output_policy.preview_bytes > self._max_output_bytes:
            raise MishkanError(
                ErrorCode.EXECUTION,
                "requested execution preview exceeds the configured output bound",
                details={"category": "output_limit"},
            )
        if len(request.environment) + len(request.credential_environment) > (
            self._max_environment_entries
        ):
            raise MishkanError(
                ErrorCode.EXECUTION,
                "requested execution environment exceeds the configured entry bound",
                details={"category": "environment_limit"},
            )
        stdin_bytes = request.stdin.encode("utf-8") if request.stdin is not None else b""
        if len(stdin_bytes) > self._max_stdin_bytes:
            raise MishkanError(
                ErrorCode.EXECUTION,
                "requested execution stdin exceeds the configured input bound",
                details={"category": "input_limit"},
            )

    @staticmethod
    def _request(call: AdapterCall) -> ExecutionRequest:
        try:
            return ExecutionRequest.model_validate(
                {**call.arguments, "execution_id": call.execution_id}
            )
        except ValidationError as exc:
            raise MishkanError(
                ErrorCode.EXECUTION,
                "direct process request is invalid",
                details={"category": "validation", "violations": len(exc.errors())},
            ) from exc

    @staticmethod
    def _executable(request: ExecutionRequest, call: AdapterCall) -> Path:
        assert request.executable is not None
        resolved = DirectProcessAdapter._canonical_executable(request.executable, call)
        if len(call.targets.executables) != 1:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "direct process received unexpected executable targets",
            )
        return resolved

    @staticmethod
    def _canonical_executable(value: str, call: AdapterCall) -> Path:
        requested = Path(value)
        try:
            resolved = requested.resolve(strict=True)
        except OSError as exc:
            raise MishkanError(
                ErrorCode.TOOL_UNAVAILABLE,
                "direct process executable is unavailable",
                details={"executable": value},
            ) from exc
        if (
            resolved != requested
            or not call.targets.executables
            or call.targets.executables[0] != str(resolved)
        ):
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "direct process executable differs from its authorized canonical target",
                details={"executable": value},
            )
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise MishkanError(
                ErrorCode.TOOL_UNAVAILABLE,
                "direct process target is not an executable regular file",
                details={"executable": value},
            )
        return resolved

    @staticmethod
    def _cwd(call: AdapterCall) -> Path:
        if len(call.targets.paths) != 1:
            raise MishkanError(
                ErrorCode.EXECUTION,
                "direct process requires one existing workspace directory",
                details={"category": "invalid_cwd"},
            )
        return DirectProcessAdapter._first_workspace_directory(call)

    @staticmethod
    def _first_workspace_directory(call: AdapterCall) -> Path:
        if not call.targets.paths or not call.targets.paths[0].absolute.is_dir():
            raise MishkanError(
                ErrorCode.EXECUTION,
                "execution requires an existing workspace directory as its first path",
                details={"category": "invalid_cwd"},
            )
        return call.targets.paths[0].absolute

    def _environment(self, request: ExecutionRequest, call: AdapterCall) -> dict[str, str]:
        environment = dict(request.environment)
        for name, reference in request.credential_environment.items():
            if not isinstance(reference, str):
                raise MishkanError(
                    ErrorCode.EXECUTION,
                    "direct process credential binding has an invalid type",
                )
            value = call.credentials.get(reference)
            if value is None:
                raise MishkanError(
                    ErrorCode.TOOL_UNAVAILABLE,
                    "authorized process credential could not be resolved",
                    details={"reference": reference},
                )
            environment[name] = value
        if any("\x00" in name or "\x00" in value for name, value in environment.items()):
            raise MishkanError(
                ErrorCode.EXECUTION,
                "direct process environment contains a null byte",
                details={"category": "invalid_environment"},
            )
        return environment

    def _communicate(
        self,
        process: subprocess.Popen[bytes],
        stdin: bytes,
        *,
        timeout_seconds: int,
        started_monotonic: float,
        cancellation_requested: Callable[[], bool],
    ) -> tuple[bytes, bytes, str | None]:
        selector = selectors.DefaultSelector()
        assert process.stdout is not None
        assert process.stderr is not None
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        for stream, channel in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, channel)
        stdin_offset = 0
        if stdin:
            assert process.stdin is not None
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        termination_cause: str | None = None
        captured = 0
        try:
            while selector.get_map():
                if termination_cause is None and cancellation_requested():
                    termination_cause = "cancellation"
                    self._stop(process)
                if (
                    termination_cause is None
                    and time.monotonic() - started_monotonic >= timeout_seconds
                ):
                    termination_cause = "timeout"
                    self._stop(process)
                events = selector.select(0.05)
                for key, _ in events:
                    channel = str(key.data)
                    file_object: Any = key.fileobj
                    if channel == "stdin":
                        descriptor = self._fileno(file_object)
                        try:
                            written = os.write(
                                descriptor, stdin[stdin_offset : stdin_offset + 8192]
                            )
                        except BrokenPipeError:
                            written = len(stdin) - stdin_offset
                        stdin_offset += written
                        if stdin_offset >= len(stdin):
                            self._unregister_and_close(selector, file_object)
                        continue
                    descriptor = self._fileno(file_object)
                    chunk = os.read(descriptor, 8192)
                    if not chunk:
                        self._unregister_and_close(selector, file_object)
                        continue
                    remaining = max(0, self._max_output_bytes - captured)
                    buffers[channel].extend(chunk[:remaining])
                    captured += min(len(chunk), remaining)
                    if len(chunk) > remaining and termination_cause is None:
                        termination_cause = "output_limit"
                        self._stop(process)
                if process.poll() is not None and not events:
                    for key in tuple(selector.get_map().values()):
                        if str(key.data) == "stdin":
                            self._unregister_and_close(selector, key.fileobj)
            if process.poll() is None:
                process.wait(timeout=1)
        finally:
            selector.close()
            if process.poll() is None:
                self._stop(process)
        return bytes(buffers["stdout"]), bytes(buffers["stderr"]), termination_cause

    @staticmethod
    def _fileno(file_object: Any) -> int:
        return file_object if isinstance(file_object, int) else int(file_object.fileno())

    @staticmethod
    def _unregister_and_close(
        selector: selectors.BaseSelector,
        file_object: Any,
    ) -> None:
        selector.unregister(file_object)
        if not isinstance(file_object, int):
            file_object.close()

    @staticmethod
    def _stop(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=0.5)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=1)

    @staticmethod
    def _preview(content: bytes, limit: int) -> str:
        return content[:limit].decode("utf-8", errors="replace")

    @staticmethod
    def _artifact_candidates(
        request: ExecutionRequest,
        stdout: bytes,
        stderr: bytes,
        termination_cause: str | None,
    ) -> tuple[ArtifactCandidate, ...]:
        output_policy = request.output_policy
        assert output_policy is not None
        if not output_policy.preserve_full_output_as_artifact:
            return ()
        return tuple(
            ArtifactCandidate(
                channel=channel,
                media_type="text/plain; charset=utf-8",
                content=content,
                complete=termination_cause != "output_limit",
            )
            for channel, content in (("stdout", stdout), ("stderr", stderr))
            if len(content) > output_policy.preview_bytes
        )

    @staticmethod
    def _status(
        cause: str | None,
        return_code: int,
        expected_exit_codes: tuple[int, ...],
    ) -> ExecutionStatus:
        if cause == "cancellation":
            return ExecutionStatus.CANCELLED
        if cause == "timeout":
            return ExecutionStatus.TIMED_OUT
        if cause == "output_limit":
            return ExecutionStatus.FAILED
        return (
            ExecutionStatus.COMPLETED
            if return_code in expected_exit_codes
            else ExecutionStatus.FAILED
        )

    @staticmethod
    def _outer_status(
        status: ExecutionStatus,
        cause: str | None,
        declared_effects: tuple[str, ...],
    ) -> CallStatus:
        if status is ExecutionStatus.CANCELLED:
            return CallStatus.CANCELLED
        if (
            cause in {"timeout", "output_limit"}
            or declared_effects
            or status in {ExecutionStatus.LOST, ExecutionStatus.UNCERTAIN}
        ):
            return CallStatus.UNCERTAIN
        if status is ExecutionStatus.FAILED:
            return CallStatus.FAILED
        return CallStatus.COMPLETED

    @staticmethod
    def _error(status: ExecutionStatus, cause: str | None, return_code: int) -> str | None:
        if status is ExecutionStatus.COMPLETED:
            return None
        if cause is not None:
            return cause
        if return_code < 0:
            return "signal_termination"
        return "unexpected_exit_code"


class BashShellAdapter(DirectProcessAdapter):
    """Run one governed Bash script under an explicit versioned profile."""

    adapter_id = "native.shell.bash"

    def __init__(
        self,
        *,
        max_output_bytes: int,
        max_stdin_bytes: int,
        max_environment_entries: int,
        max_script_bytes: int,
        max_startup_file_bytes: int,
        execution_location: str = "local",
    ) -> None:
        super().__init__(
            max_output_bytes=max_output_bytes,
            max_stdin_bytes=max_stdin_bytes,
            max_environment_entries=max_environment_entries,
            execution_location=execution_location,
        )
        if min(max_script_bytes, max_startup_file_bytes) < 1:
            raise ValueError("Bash script and startup-file bounds must be positive")
        self._max_script_bytes = max_script_bytes
        self._max_startup_file_bytes = max_startup_file_bytes

    def invoke(self, call: AdapterCall) -> AdapterResult:
        request = self._request(call)
        self._validate_constraints(request, call)
        assert request.timeout_seconds is not None
        assert request.output_policy is not None
        assert request.shell_profile is not None
        assert request.script is not None
        profile = request.shell_profile
        executable = self._canonical_executable(profile.interpreter, call)
        self._validate_declared_executables(call)
        cwd = self._first_workspace_directory(call)
        startup_paths = self._startup_paths(profile, call)
        effective_script = self._effective_script(profile, startup_paths, request.script)
        stdin = request.stdin.encode("utf-8") if request.stdin is not None else b""
        environment = self._environment(request, call)
        argv = self._argv(executable, profile, effective_script)
        started_at = utc_now()
        started_monotonic = time.monotonic()
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment,
                stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise MishkanError(
                ErrorCode.EXECUTION,
                "Bash shell could not be spawned",
                details={"category": "spawn_failure", "reason": type(exc).__name__},
            ) from exc
        stdout, stderr, termination_cause = self._communicate(
            process,
            stdin,
            timeout_seconds=request.timeout_seconds,
            started_monotonic=started_monotonic,
            cancellation_requested=call.cancellation_requested,
        )
        return_code = process.returncode
        if return_code is None:
            raise MishkanError(
                ErrorCode.EXECUTION,
                "Bash shell did not settle after termination",
                details={"category": "lost_process"},
            )
        status = self._status(termination_cause, return_code, request.expected_exit_codes)
        effect_settlement = (
            EffectSettlement.ABSENT if not request.declared_effects else EffectSettlement.UNCERTAIN
        )
        outer_status = self._outer_status(status, termination_cause, request.declared_effects)
        result = ExecutionResult(
            execution_id=UUID(call.execution_id),
            mode=ExecutionMode.SHELL,
            status=status,
            executable=str(executable),
            args=(),
            cwd=call.targets.paths[0].relative,
            exit_code=return_code if return_code >= 0 else None,
            signal=-return_code if return_code < 0 else None,
            started_at=started_at,
            finished_at=utc_now(),
            stdout_preview=self._preview(stdout, request.output_policy.preview_bytes),
            stderr_preview=self._preview(stderr, request.output_policy.preview_bytes),
            stdout_bytes=len(stdout),
            stderr_bytes=len(stderr),
            stdout_digest=f"sha256:{hashlib.sha256(stdout).hexdigest()}",
            stderr_digest=f"sha256:{hashlib.sha256(stderr).hexdigest()}",
            truncated=(
                len(stdout) > request.output_policy.preview_bytes
                or len(stderr) > request.output_policy.preview_bytes
                or termination_cause == "output_limit"
            ),
            termination_cause=termination_cause,
            expected_exit_codes=request.expected_exit_codes,
            environment_names=tuple(sorted(request.environment)),
            credential_environment_names=tuple(sorted(request.credential_environment)),
            declared_effects=request.declared_effects,
            effect_settlement=effect_settlement,
            execution_location=self._execution_location,
            error=self._error(status, termination_cause, return_code),
        )
        profile_payload = json.dumps(
            profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return AdapterResult(
            output=result.model_dump(mode="json"),
            actual_targets=call.targets,
            evidence={
                "adapter": self.adapter_id,
                "shell": True,
                "dialect": profile.dialect.value,
                "profile_id": profile.profile_id,
                "profile_revision": profile.revision,
                "profile_fingerprint": f"sha256:{hashlib.sha256(profile_payload).hexdigest()}",
                "interpreter": str(executable),
                "startup_mode": "no_profile_no_rc",
                "startup_files": [
                    path.relative for path in call.targets.paths[1 : 1 + len(profile.startup_files)]
                ],
                "shell_options": profile.options.model_dump(mode="json"),
                "script_digest": f"sha256:{hashlib.sha256(request.script.encode()).hexdigest()}",
                "process_group": "isolated",
                "ambient_environment": False,
                "memory_limit_mb": call.resources.memory_mb,
                "network_denial_enforced": False,
                "isolation_profile": call.isolation_profile,
            },
            inspection_content=(
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            ),
            artifact_candidates=self._artifact_candidates(
                request, stdout, stderr, termination_cause
            ),
            call_status=outer_status,
            retryable=False,
            error_code=(
                ErrorCode.EXECUTION.value if outer_status is not CallStatus.COMPLETED else None
            ),
            reason=f"Bash shell settled as {status.value}",
        )

    def _validate_constraints(self, request: ExecutionRequest, call: AdapterCall) -> None:
        if request.mode is not ExecutionMode.SHELL:
            raise MishkanError(
                ErrorCode.EXECUTION,
                "Bash adapter received a non-shell execution mode",
                details={"category": "invalid_mode"},
            )
        self._validate_common_constraints(request, call)
        assert request.script is not None
        if len(request.script.encode("utf-8")) > self._max_script_bytes:
            raise MishkanError(
                ErrorCode.EXECUTION,
                "Bash script exceeds the configured input bound",
                details={"category": "input_limit"},
            )

    def _startup_paths(self, profile: ShellProfile, call: AdapterCall) -> tuple[Path, ...]:
        resolved = call.targets.paths[1 : 1 + len(profile.startup_files)]
        if len(resolved) != len(profile.startup_files):
            raise MishkanError(
                ErrorCode.TOOL_EFFECT,
                "resolved Bash startup files differ from the selected profile",
            )
        paths: list[Path] = []
        for expected, target in zip(profile.startup_files, resolved, strict=True):
            if target.requested != expected or not target.absolute.is_file():
                raise MishkanError(
                    ErrorCode.TOOL_UNAVAILABLE,
                    "Bash startup file is unavailable",
                    details={"path": expected},
                )
            if target.absolute.stat().st_size > self._max_startup_file_bytes:
                raise MishkanError(
                    ErrorCode.EXECUTION,
                    "Bash startup file exceeds the configured input bound",
                    details={"category": "input_limit", "path": expected},
                )
            paths.append(target.absolute)
        return tuple(paths)

    @staticmethod
    def _validate_declared_executables(call: AdapterCall) -> None:
        for value in call.targets.executables[1:]:
            requested = Path(value)
            try:
                resolved = requested.resolve(strict=True)
            except OSError as exc:
                raise MishkanError(
                    ErrorCode.TOOL_UNAVAILABLE,
                    "declared Bash executable is unavailable",
                    details={"executable": value},
                ) from exc
            if resolved != requested or not resolved.is_file() or not os.access(resolved, os.X_OK):
                raise MishkanError(
                    ErrorCode.AUTHORITY_NOT_GRANTED,
                    "declared Bash executable is not a canonical executable file",
                    details={"executable": value},
                )

    @staticmethod
    def _effective_script(
        profile: ShellProfile,
        startup_paths: tuple[Path, ...],
        script: str,
    ) -> str:
        prelude = [
            'if [ -z "${BASH_VERSION-}" ]; then '
            "printf '%s\\n' 'configured interpreter is not Bash' >&2; exit 2; fi"
        ]
        if profile.options.inherit_errexit:
            prelude.append(
                "shopt -s inherit_errexit 2>/dev/null || { "
                "printf '%s\\n' 'inherit_errexit is unsupported' >&2; exit 2; }"
            )
        prelude.extend(f"source -- {shlex.quote(str(path))}" for path in startup_paths)
        return "\n".join((*prelude, script))

    @staticmethod
    def _argv(executable: Path, profile: ShellProfile, script: str) -> tuple[str, ...]:
        options = profile.options
        return (
            str(executable),
            "--noprofile",
            "--norc",
            "-o" if options.pipefail else "+o",
            "pipefail",
            "-o" if options.errexit else "+o",
            "errexit",
            "-o" if options.nounset else "+o",
            "nounset",
            "-c",
            script,
            "mishkan-shell",
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
