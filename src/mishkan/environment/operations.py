"""Deterministic construction of governed execution requests from public adapter profiles."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from mishkan.artifacts import ArtifactManifest
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.environment.models import (
    AvailabilityState,
    EnvironmentBindingState,
    EnvironmentDescriptorSet,
    EnvironmentOperationPlan,
    EnvironmentOperationRequest,
)
from mishkan.environment.profile import EnvironmentAdapterDefinition, EnvironmentProfile
from mishkan.environment.repository import SQLiteEnvironmentRepository
from mishkan.tools.execution import (
    ExecutionMode,
    ExecutionRequest,
    OutputPolicy,
    ReadinessProbe,
)


class EnvironmentOperationPlanner:
    """Bind a compatible adapter to literal argv; execution remains owned by I03."""

    def __init__(
        self,
        profile: EnvironmentProfile,
        repository: SQLiteEnvironmentRepository,
        artifacts: EnvironmentOperationArtifactReader,
    ) -> None:
        self._profile = profile
        self._repository = repository
        self._artifacts = artifacts
        self._adapters = {item.adapter_id: item for item in profile.adapters}

    def plan(self, request: EnvironmentOperationRequest) -> EnvironmentOperationPlan:
        binding = self._repository.binding(str(request.binding_id))
        if binding.state is not EnvironmentBindingState.COMPATIBLE:
            raise MishkanError(
                ErrorCode.ENGINEERING,
                "environment operation requires a compatible binding",
            )
        if request.adapter_id not in binding.selected_adapter_ids:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "environment adapter is not selected by the binding",
            )
        adapter = self._adapters.get(request.adapter_id)
        if adapter is None:
            raise MishkanError(ErrorCode.TOOL_UNAVAILABLE, "environment adapter is not configured")
        definition = adapter.operations.get(request.operation.value)
        if definition is None:
            raise MishkanError(
                ErrorCode.TOOL_UNAVAILABLE,
                "environment adapter does not implement the requested operation",
                details={"adapter": adapter.adapter_id, "operation": request.operation.value},
            )
        observation = self._repository.observation(str(binding.request.observation_id))
        observed_engines = {item.engine_id: item for item in observation.engines}
        engine = next(
            (
                item
                for item in observation.engines
                if item.engine_id == adapter.engine_id and item.adapter_id == adapter.adapter_id
            ),
            None,
        )
        if (
            engine is None
            or engine.executable_path is None
            or engine.fact("eligible") is not AvailabilityState.TRUE
        ):
            raise MishkanError(
                ErrorCode.TOOL_UNAVAILABLE,
                "environment adapter executable is not currently eligible",
            )
        path_engines = tuple(observed_engines.get(item) for item in definition.path_engine_ids)
        if any(
            dependency is None
            or dependency.executable_path is None
            or dependency.fact("eligible") is not AvailabilityState.TRUE
            for dependency in path_engines
        ):
            raise MishkanError(
                ErrorCode.TOOL_UNAVAILABLE,
                "environment adapter path dependency is not currently eligible",
            )
        descriptor_set = self._descriptor_set(request, adapter)
        descriptor_path = self._descriptor_path(
            request,
            descriptor_set,
            adapter,
            observation.workspace,
        )
        replacements = {f"${name}": value for name, value in request.parameters.items()}
        if descriptor_path is not None:
            replacements["$descriptor"] = descriptor_path
        missing = sorted(set(definition.required_parameters) - set(request.parameters))
        if "$descriptor" in definition.arguments and descriptor_path is None:
            missing.append("descriptor")
        if missing:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "environment operation parameters are incomplete",
                details={"missing": tuple(missing)},
            )
        accepted_tokens = {
            token.removeprefix("$") for token in definition.arguments if token.startswith("$")
        }
        unexpected = sorted(set(request.parameters) - accepted_tokens)
        if unexpected:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "environment operation contains unused parameters",
                details={"unexpected": tuple(unexpected)},
            )
        arguments = tuple(replacements.get(value, value) for value in definition.arguments)
        if any(value.startswith("$") for value in arguments):
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "environment operation contains an unresolved profile token",
            )
        mode = ExecutionMode(definition.mode)
        if mode is ExecutionMode.PROCESS and definition.declared_effects:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "process-mode environment adapter cannot declare stateful effects",
            )
        readiness = None
        if definition.readiness is not None:
            readiness = ReadinessProbe(
                kind=definition.readiness,
                value=definition.readiness_value,
                timeout_seconds=min(request.timeout_seconds, 3_600),
            )
        declared_paths = (descriptor_path,) if descriptor_path is not None else ()
        common: dict[str, object] = {
            "execution_id": request.operation_id,
            "mode": mode,
            "cwd": ".",
            "executable": str(engine.executable_path),
            "args": arguments,
            "timeout_seconds": request.timeout_seconds,
            "expected_exit_codes": request.expected_exit_codes,
            "declared_paths": declared_paths,
            "declared_executables": (str(engine.executable_path),),
            "network_destinations": request.network_destinations,
            "declared_effects": definition.declared_effects,
            "environment": {
                **request.environment,
                **(
                    {
                        "PATH": ":".join(
                            dict.fromkeys(
                                (
                                    str(engine.executable_path.parent),
                                    *(
                                        str(dependency.executable_path.parent)
                                        for dependency in path_engines
                                        if dependency is not None
                                        and dependency.executable_path is not None
                                    ),
                                )
                            )
                        )
                    }
                    if definition.path_engine_ids
                    else {}
                ),
            },
            "output_policy": OutputPolicy(
                preview_bytes=request.preview_bytes,
                preserve_full_output_as_artifact=True,
            ),
        }
        if mode is ExecutionMode.PROCESS:
            if request.credential_environment or request.credential_references:
                raise MishkanError(
                    ErrorCode.OUTPUT_CONTRACT,
                    "direct environment probe cannot receive unresolved credential references",
                )
        else:
            common.update(
                {
                    "credential_environment": request.credential_environment,
                    "credential_references": request.credential_references,
                    "owner": request.owner_identity,
                    "run_id": request.run_id,
                    "task_id": request.task_id,
                    "session_profile": request.session_profile,
                    "deadline": request.deadline,
                    "readiness": readiness,
                    "policy_fingerprint": "0" * 64,
                }
            )
        execution = ExecutionRequest.model_validate(common)
        return EnvironmentOperationPlan(
            request=request,
            binding_revision=binding.revision,
            observation_fingerprint=observation.fingerprint,
            profile_id=self._profile.profile_id,
            profile_revision=self._profile.revision,
            adapter_revision=adapter.revision,
            execution=execution,
        )

    def _descriptor_set(
        self,
        request: EnvironmentOperationRequest,
        adapter: EnvironmentAdapterDefinition,
    ) -> EnvironmentDescriptorSet | None:
        if request.descriptor_set_id is None:
            return None
        descriptor_set = self._repository.descriptor_set(str(request.descriptor_set_id))
        if descriptor_set.binding_id != request.binding_id:
            raise MishkanError(
                ErrorCode.REVISION_MISMATCH,
                "environment descriptor set belongs to another binding",
            )
        if not any(item.format in adapter.descriptor_formats for item in descriptor_set.members):
            raise MishkanError(
                ErrorCode.TOOL_UNAVAILABLE,
                "environment adapter is incompatible with the descriptor set",
            )
        return descriptor_set

    def _descriptor_path(
        self,
        request: EnvironmentOperationRequest,
        descriptor_set: EnvironmentDescriptorSet | None,
        adapter: EnvironmentAdapterDefinition,
        workspace: Path,
    ) -> str | None:
        if request.descriptor_path is None:
            return None
        if descriptor_set is None:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "descriptor path requires an exact descriptor set",
            )
        matches = [
            item
            for item in descriptor_set.members
            if item.logical_path == request.descriptor_path
            and item.format in adapter.descriptor_formats
        ]
        if len(matches) != 1:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "descriptor path is not an exact compatible descriptor-set member",
            )
        member = matches[0]
        candidate = workspace / request.descriptor_path
        if candidate.is_symlink() or not candidate.is_file():
            raise MishkanError(
                ErrorCode.FILE,
                "environment descriptor has not been materialized as a plain project file",
            )
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(workspace):
            raise MishkanError(ErrorCode.FILE, "environment descriptor escapes the workspace")
        manifest = self._artifacts.manifest(member.artifact_reference)
        digest = f"sha256:{hashlib.sha256(resolved.read_bytes()).hexdigest()}"
        if digest != manifest.digest:
            raise MishkanError(
                ErrorCode.REVISION_MISMATCH,
                "materialized environment descriptor differs from its accepted artifact",
            )
        return request.descriptor_path


class EnvironmentOperationArtifactReader(Protocol):
    def manifest(self, reference: str) -> ArtifactManifest: ...
