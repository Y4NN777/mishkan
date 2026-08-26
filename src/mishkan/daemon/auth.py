"""Local bearer-token creation, validation, and rotation."""

from __future__ import annotations

import hmac
import os
import secrets
import stat
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mishkan.domain.errors import ErrorCode, MishkanError


class TokenRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    principal_id: str = Field(min_length=1, max_length=256)
    token: str = Field(min_length=43)


class TokenFile:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def create(self, principal_id: str, *, replace: bool = False) -> TokenRecord:
        if self.path.exists() and not replace:
            return self.read()
        record = TokenRecord(principal_id=principal_id, token=secrets.token_urlsafe(32))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, staged_name = tempfile.mkstemp(prefix=".api-token.", dir=self.path.parent)
        staged = Path(staged_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(record.model_dump_json().encode())
                stream.flush()
                os.fsync(stream.fileno())
            if self.path.exists() and not replace:
                raise MishkanError(
                    ErrorCode.AUTHORITY_NOT_GRANTED,
                    "daemon token appeared concurrently; explicit rotation is required",
                )
            os.replace(staged, self.path)
            os.chmod(self.path, 0o600)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            staged.unlink(missing_ok=True)
        return record

    def rotate(self, principal_id: str | None = None) -> TokenRecord:
        current = self.read()
        return self.create(principal_id or current.principal_id, replace=True)

    def read(self) -> TokenRecord:
        try:
            metadata = self.path.stat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise MishkanError(
                    ErrorCode.AUTHORITY_NOT_GRANTED,
                    "daemon token file must be a regular file with mode 0600",
                    details={"path": str(self.path)},
                )
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise MishkanError(
                    ErrorCode.AUTHORITY_NOT_GRANTED,
                    "daemon token file owner differs from the current process",
                    details={"path": str(self.path)},
                )
            return TokenRecord.model_validate_json(self.path.read_text(encoding="utf-8"))
        except MishkanError:
            raise
        except (OSError, ValidationError) as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "daemon token file is missing or invalid",
                details={"path": str(self.path)},
            ) from exc

    def authenticate(self, candidate: str) -> TokenRecord | None:
        record = self.read()
        return record if hmac.compare_digest(candidate, record.token) else None

    def public_status(self) -> dict[str, str]:
        record = self.read()
        return {"path": str(self.path), "principal_id": record.principal_id}
