"""Public contracts for deterministic, layered task context."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.domain.identity import new_id
from mishkan.domain.time import require_aware, utc_now
from mishkan.skills.models import SkillLoadEvidence, SkillUseOutcome

_DIGEST_PATTERN = r"^sha256:[a-f0-9]{64}$"
_ARTIFACT_PATTERN = r"^artifact:[0-9a-f-]{36}$"


class ContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContextPackEntry(ContextModel):
    """One immutable artifact projected at a safe logical path."""

    logical_path: str = Field(min_length=1, max_length=512)
    layer: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    order: int = Field(ge=0, le=1_000_000)
    artifact_reference: str = Field(pattern=_ARTIFACT_PATTERN)
    digest: str = Field(pattern=_DIGEST_PATTERN)
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=256)
    sensitivity: str = Field(min_length=1, max_length=128)
    required: bool = True
    source_revision: str | None = Field(default=None, min_length=1, max_length=512)

    @field_validator("logical_path")
    @classmethod
    def logical_path_is_safe_and_normalized(cls, value: str) -> str:
        if "\\" in value or "\x00" in value:
            raise ValueError("context logical path must use safe POSIX separators")
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError("context logical path must use NFC Unicode normalization")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("context logical path must be normalized and relative")
        if path.parts[0] == ".mishkan":
            raise ValueError("context logical path uses the reserved manifest namespace")
        normalized = path.as_posix()
        if normalized != value:
            raise ValueError("context logical path must be normalized and relative")
        return value


class ContextPackManifest(ContextModel):
    """Authoritative description of a bounded context projection."""

    schema_version: Literal["1.0"] = "1.0"
    context_pack_id: UUID = Field(default_factory=new_id)
    created_at: datetime = Field(default_factory=utc_now)
    organization_revision: str = Field(min_length=1, max_length=512)
    agent_identity: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=256)
    mission_id: str | None = Field(default=None, min_length=1, max_length=256)
    mission_brief_revision: str | None = Field(default=None, min_length=1, max_length=512)
    plan_revision: str | None = Field(default=None, min_length=1, max_length=512)
    output_contract_reference: str = Field(pattern=_ARTIFACT_PATTERN)
    output_contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    verification_contract_reference: str = Field(pattern=_ARTIFACT_PATTERN)
    verification_contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    max_entries: int = Field(ge=1, le=10_000)
    max_entry_bytes: int = Field(ge=1, le=1_073_741_824)
    max_total_bytes: int = Field(ge=1, le=4_294_967_296)
    entries: tuple[ContextPackEntry, ...] = Field(min_length=1)
    skill_loads: tuple[SkillLoadEvidence, ...] = ()

    @field_validator("created_at")
    @classmethod
    def created_at_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def entries_are_unique_ordered_and_bounded(self) -> ContextPackManifest:
        if len(self.entries) > self.max_entries:
            raise ValueError("context pack exceeds its entry-count bound")
        paths = [entry.logical_path for entry in self.entries]
        if len(paths) != len(set(paths)):
            raise ValueError("context pack logical paths must be unique")
        if len(paths) != len({path.casefold() for path in paths}):
            raise ValueError("context pack logical paths must be portable across case rules")
        parts = [PurePosixPath(path).parts for path in paths]
        if any(
            left != right and len(left) < len(right) and right[: len(left)] == left
            for left in parts
            for right in parts
        ):
            raise ValueError("context pack file paths must not contain another file path")
        order_keys = [(entry.order, entry.logical_path) for entry in self.entries]
        if order_keys != sorted(order_keys):
            raise ValueError("context pack entries must use deterministic order")
        if any(entry.size_bytes > self.max_entry_bytes for entry in self.entries):
            raise ValueError("context pack entry exceeds its byte bound")
        if sum(entry.size_bytes for entry in self.entries) > self.max_total_bytes:
            raise ValueError("context pack exceeds its total byte bound")
        required_identities = {
            (entry.artifact_reference, entry.digest) for entry in self.entries if entry.required
        }
        if (self.output_contract_reference, self.output_contract_digest) not in required_identities:
            raise ValueError("context pack must materialize its exact output contract")
        if (
            self.verification_contract_reference,
            self.verification_contract_digest,
        ) not in required_identities:
            raise ValueError("context pack must materialize its exact verification contract")
        digests = {entry.digest for entry in self.entries}
        skill_names: set[str] = set()
        for load in self.skill_loads:
            if load.task_id != self.task_id or load.consuming_identity != self.agent_identity:
                raise ValueError(
                    "context skill evidence must belong to its consuming task and identity"
                )
            if load.skill_name in skill_names:
                raise ValueError("context pack must not contain duplicate loaded skill identities")
            skill_names.add(load.skill_name)
            if load.outcome is SkillUseOutcome.MISS or load.instruction_fingerprint is None:
                raise ValueError("context pack may include only loaded hit or partial skills")
            loaded_digests = {
                load.instruction_fingerprint,
                *(resource.fingerprint for resource in load.loaded_resources),
            }
            if not loaded_digests.issubset(digests):
                raise ValueError("context pack must materialize every loaded skill content item")
        return self

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def fingerprint(self) -> str:
        return f"sha256:{hashlib.sha256(self.canonical_bytes()).hexdigest()}"


class MaterializedContextEntry(ContextModel):
    logical_path: str = Field(min_length=1, max_length=512)
    artifact_reference: str = Field(pattern=_ARTIFACT_PATTERN)
    digest: str = Field(pattern=_DIGEST_PATTERN)
    size_bytes: int = Field(ge=0)


class ContextPackMaterialization(ContextModel):
    schema_version: Literal["1.0"] = "1.0"
    context_pack_id: UUID
    manifest_fingerprint: str = Field(pattern=_DIGEST_PATTERN)
    tree_digest: str = Field(pattern=_DIGEST_PATTERN)
    entries: tuple[MaterializedContextEntry, ...]
    omitted_optional_paths: tuple[str, ...] = ()
    total_bytes: int = Field(ge=0)
