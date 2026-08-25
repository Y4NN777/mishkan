"""Typed capability adapter ports and native filesystem implementations."""

from __future__ import annotations

import base64
import difflib
import hashlib
import stat
import subprocess
from dataclasses import dataclass
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
