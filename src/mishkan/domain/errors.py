"""Stable machine-readable failure contracts."""

from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field

from mishkan.domain.identity import DomainRecord


class ErrorCode(StrEnum):
    CONFIGURATION = "ERR-CFG-001"
    PROJECT = "ERR-PRJ-001"
    PLAN = "ERR-PLN-001"
    AUTHORIZATION_MISSING = "ERR-PLN-002"
    DECISION_INSUFFICIENT = "ERR-DEC-001"
    DECISION_VALIDATION = "ERR-DEC-002"
    AUTHORITY_NOT_GRANTED = "ERR-POL-001"
    POLICY_CONFLICT = "ERR-POL-002"
    ROLE_CONFLICT = "ERR-ROL-001"
    OUTPUT_CONTRACT = "ERR-OUT-001"
    REVISION_MISMATCH = "ERR-REV-001"
    RUN_INTERRUPTED = "ERR-RUN-001"
    DUPLICATE_RESULT = "ERR-RUN-002"
    OPTIONAL_DEPENDENCY = "ERR-DEP-001"
    REQUIRED_DEPENDENCY = "ERR-DEP-002"
    SECRET_CONTENT = "ERR-SEC-001"
    SKILL_CONTRACT = "ERR-SKL-001"
    SKILL_TRUST = "ERR-SKL-002"
    SKILL_SELECTION = "ERR-SKL-003"
    TOOL_CONTRACT = "ERR-TOL-001"
    TOOL_UNAVAILABLE = "ERR-TOL-002"
    TOOL_SCHEMA = "ERR-TOL-003"
    TOOL_EFFECT = "ERR-TOL-004"
    TOOL_DRIFT = "ERR-TOL-005"
    CONTEXT = "ERR-CTX-001"
    MISSION = "ERR-MSN-001"
    FILE = "ERR-FIL-001"
    EDIT = "ERR-EDT-001"
    EXECUTION = "ERR-EXE-001"
    WEB = "ERR-WEB-001"
    BROWSER = "ERR-BRW-001"
    ARTIFACT = "ERR-ART-001"
    MCP = "ERR-MCP-001"
    ENGINEERING = "ERR-ENG-001"
    SCHEDULE = "ERR-SCH-001"
    WORKER = "ERR-WRK-001"
    VERSION = "ERR-VER-001"


class ErrorEnvelope(DomainRecord):
    """Versioned, serializable failure returned by every public interface."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False


class MishkanError(Exception):
    """Expected failure carrying a stable error envelope."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.envelope = ErrorEnvelope(
            code=code,
            message=message,
            details=details or {},
            retryable=retryable,
        )
