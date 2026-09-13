"""Compose validated descriptor artifacts into exact-base I03 change sets."""

from __future__ import annotations

from pathlib import PurePosixPath

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.edits import (
    ChangeOperation,
    ChangeOperationKind,
    ChangeSet,
    ChangeValidation,
    ChangeValidationKind,
    PreconditionKind,
)
from mishkan.environment.models import (
    EnvironmentDescriptorChangePlan,
    EnvironmentDescriptorChangeRequest,
)
from mishkan.environment.repository import SQLiteEnvironmentRepository


class EnvironmentDescriptorChangePlanner:
    """Plan persistence without authoring descriptor content or applying an effect."""

    def __init__(self, repository: SQLiteEnvironmentRepository) -> None:
        self._repository = repository

    def plan(
        self,
        request: EnvironmentDescriptorChangeRequest,
    ) -> EnvironmentDescriptorChangePlan:
        descriptor_set = self._repository.descriptor_set(str(request.descriptor_set_id))
        binding = self._repository.binding(str(descriptor_set.binding_id))
        if binding.request.owner_identity != request.owner_identity:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "environment descriptor change owner differs from its binding owner",
            )
        observation = self._repository.observation(str(binding.request.observation_id))
        existing = {item.logical_path: item for item in observation.descriptors}
        operations: list[ChangeOperation] = []
        validations: list[ChangeValidation] = []
        unchanged: list[str] = []
        for member in descriptor_set.members:
            if (
                member.base_revision is not None
                and member.base_revision != observation.repository_revision
            ):
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH,
                    "environment descriptor base revision is stale",
                    details={"path": member.logical_path},
                )
            manifest = self._repository.artifact_manifest(member.artifact_reference)
            current = existing.get(member.logical_path)
            if current is not None and current.digest == manifest.digest:
                unchanged.append(member.logical_path)
                continue
            if current is not None:
                kind = ChangeOperationKind.WRITE
                precondition = PreconditionKind.DIGEST
                precondition_value = current.digest
                result_mode = None
            else:
                kind = ChangeOperationKind.CREATE
                precondition = PreconditionKind.ABSENT
                precondition_value = None
                result_mode = request.result_mode
            operations.append(
                ChangeOperation(
                    kind=kind,
                    path=member.logical_path,
                    precondition=precondition,
                    precondition_value=precondition_value,
                    artifact_reference=member.artifact_reference,
                    expected_digest=manifest.digest,
                    result_mode=result_mode,
                )
            )
            validations.append(
                ChangeValidation(
                    kind=ChangeValidationKind.DIGEST,
                    path=member.logical_path,
                    expected_value=manifest.digest,
                )
            )
        change_set = None
        if operations:
            parents = {PurePosixPath(item.path).parent.as_posix() or "." for item in operations}
            change_set = ChangeSet(
                workspace=".",
                scope=f"environment-descriptor:{descriptor_set.descriptor_set_id}",
                path_scopes=tuple(sorted(parents)),
                operations=tuple(operations),
                declared_effects=("workspace.descriptor.persist",),
                validations=tuple(validations),
            )
        return EnvironmentDescriptorChangePlan(
            request=request,
            binding_id=binding.binding_id,
            observation_fingerprint=observation.fingerprint,
            unchanged_paths=tuple(unchanged),
            change_set=change_set,
        )
