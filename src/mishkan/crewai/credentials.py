"""Late credential-pool resolution without serializing secret values."""

from __future__ import annotations

import importlib
import os
import shlex
import subprocess
from pathlib import Path

from mishkan.config.models import CredentialReference, CredentialSource
from mishkan.domain.errors import ErrorCode, MishkanError


class CredentialPoolResolver:
    def resolve(self, references: tuple[CredentialReference, ...]) -> tuple[str | None, ...]:
        if not references:
            return (None,)
        resolved = tuple(value for reference in references if (value := self._resolve(reference)))
        if not resolved:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "credential pool has no resolvable entries",
                details={"sources": [reference.source.value for reference in references]},
            )
        return resolved

    def resolve_exact(
        self,
        references: tuple[CredentialReference, ...],
    ) -> dict[str, str]:
        """Resolve every named reference for a contract that requires exact mappings."""
        resolved: dict[str, str] = {}
        for reference in references:
            value = self._resolve(reference)
            if value is None:
                raise MishkanError(
                    ErrorCode.AUTHORIZATION_MISSING,
                    "required credential reference is unavailable",
                    details={"source": reference.source.value, "locator": reference.locator},
                )
            resolved[reference.locator] = value
        if len(resolved) != len(references):
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "credential references contain duplicate locators",
            )
        return resolved

    def _resolve(self, reference: CredentialReference) -> str | None:
        if reference.source is CredentialSource.ENV:
            return os.environ.get(reference.locator)
        if reference.source is CredentialSource.FILE:
            return self._read_file(Path(reference.locator))
        if reference.source is CredentialSource.COMMAND:
            return self._run_command(reference.locator)
        if reference.source is CredentialSource.KEYRING:
            return self._read_keyring(reference.locator)
        return None

    @staticmethod
    def _read_file(path: Path) -> str | None:
        try:
            value = path.expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    @staticmethod
    def _run_command(command: str) -> str | None:
        try:
            arguments = shlex.split(command)
            if not arguments:
                return None
            completed = subprocess.run(
                arguments,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
        return completed.stdout.strip() or None

    @staticmethod
    def _read_keyring(locator: str) -> str | None:
        try:
            keyring = importlib.import_module("keyring")
            service, username = locator.split(":", 1)
            value = keyring.get_password(service, username)
        except (ImportError, ValueError):
            return None
        return str(value) if value else None
