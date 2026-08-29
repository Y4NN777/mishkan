"""Versioned technical packs that turn observed project context into command candidates."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.sources import resolve_source_path
from mishkan.domain.time import require_aware
from mishkan.environment.models import AvailabilityState, EnvironmentObservation
from mishkan.tools.execution import ExecutionMode, ExecutionRequest


class PackModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PackCommandAlternative(PackModel):
    engine_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    arguments: tuple[str, ...] = ()


class PackCommandDefinition(PackModel):
    action: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    mode: ExecutionMode
    alternatives: tuple[PackCommandAlternative, ...] = Field(min_length=1)
    declared_effects: tuple[str, ...] = ()
    network: bool = False


class TechnicalPackDefinition(PackModel):
    pack_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    revision: str = Field(min_length=1, max_length=512)
    ecosystems: tuple[str, ...] = Field(min_length=1)
    marker_names: tuple[str, ...] = ()
    commands: tuple[PackCommandDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def command_actions_are_unique(self) -> TechnicalPackDefinition:
        actions = [command.action for command in self.commands]
        if len(actions) != len(set(actions)):
            raise ValueError("technical pack command actions must be unique")
        return self


class TechnicalPackCatalogue(PackModel):
    schema_version: Literal["1.0"] = "1.0"
    catalogue_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    revision: str = Field(min_length=1, max_length=512)
    packs: tuple[TechnicalPackDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def pack_identities_are_unique(self) -> TechnicalPackCatalogue:
        identities = [pack.pack_id for pack in self.packs]
        if len(identities) != len(set(identities)):
            raise ValueError("technical pack identities must be unique")
        return self


class EngineeringCommandState(StrEnum):
    READY = "ready"
    UNAVAILABLE = "unavailable"


class EngineeringCommandCandidate(PackModel):
    schema_version: Literal["1.0"] = "1.0"
    pack_id: str
    pack_revision: str
    action: str
    state: EngineeringCommandState
    engine_id: str | None = None
    executable: Path | None = None
    arguments: tuple[str, ...] = ()
    mode: ExecutionMode
    declared_effects: tuple[str, ...]
    network: bool
    evidence: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def ready_candidate_has_exact_execution(self) -> EngineeringCommandCandidate:
        exact = self.engine_id is not None and self.executable is not None
        if self.state is EngineeringCommandState.READY and not exact:
            raise ValueError("ready engineering command requires an exact executable and arguments")
        if self.state is EngineeringCommandState.UNAVAILABLE and exact:
            raise ValueError("unavailable engineering command cannot claim an exact execution")
        return self


class EngineeringCommandRequest(PackModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: UUID = Field(default_factory=uuid4)
    observation_id: UUID
    observation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    pack_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    action: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    owner_identity: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=256)
    session_profile: str = Field(min_length=1, max_length=128)
    deadline: datetime
    timeout_seconds: int = Field(ge=1, le=86_400)
    expected_exit_codes: tuple[int, ...] = (0,)

    @field_validator("deadline")
    @classmethod
    def deadline_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class EngineeringCommandPlan(PackModel):
    schema_version: Literal["1.0"] = "1.0"
    request: EngineeringCommandRequest
    candidate: EngineeringCommandCandidate
    observation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution: ExecutionRequest


class TechnicalPackLoader:
    def load(self, sources: tuple[str, ...], project_root: Path) -> TechnicalPackCatalogue:
        catalogues = tuple(self._load(source, project_root) for source in sources)
        packs: dict[str, TechnicalPackDefinition] = {}
        revisions: list[str] = []
        for catalogue in catalogues:
            revisions.append(f"{catalogue.catalogue_id}@{catalogue.revision}")
            for pack in catalogue.packs:
                if pack.pack_id in packs:
                    raise MishkanError(
                        ErrorCode.CONFIGURATION,
                        "technical pack identity is duplicated across configured sources",
                        details={"pack_id": pack.pack_id},
                    )
                packs[pack.pack_id] = pack
        return TechnicalPackCatalogue(
            catalogue_id="configured-engineering-packs",
            revision="+".join(revisions),
            packs=tuple(packs.values()),
        )

    @staticmethod
    def _load(source: str, project_root: Path) -> TechnicalPackCatalogue:
        try:
            if source.startswith("package://"):
                location = source.removeprefix("package://")
                package, separator, resource = location.rpartition("/")
                if not separator:
                    raise ValueError("package technical-pack source is invalid")
                text = files(package).joinpath(resource).read_text(encoding="utf-8")
            else:
                path = resolve_source_path(source, project_root, "technical pack catalogue")
                text = path.read_text(encoding="utf-8")
            return TechnicalPackCatalogue.model_validate(yaml.safe_load(text))
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "technical pack catalogue cannot be loaded",
                details={"source": source},
            ) from exc


class TechnicalPackService:
    def __init__(self, catalogue: TechnicalPackCatalogue) -> None:
        self._catalogue = catalogue

    def candidates(
        self,
        observation: EnvironmentObservation,
    ) -> tuple[EngineeringCommandCandidate, ...]:
        markers = {Path(path).name for values in observation.manifests.values() for path in values}
        engines = {engine.engine_id: engine for engine in observation.engines}
        candidates: list[EngineeringCommandCandidate] = []
        for pack in self._catalogue.packs:
            observed_ecosystems = tuple(
                ecosystem for ecosystem in pack.ecosystems if observation.manifests.get(ecosystem)
            )
            if not observed_ecosystems:
                continue
            if pack.marker_names and not (set(pack.marker_names) & markers):
                continue
            for command in pack.commands:
                chosen = next(
                    (
                        (alternative, engines[alternative.engine_id])
                        for alternative in command.alternatives
                        if alternative.engine_id in engines
                        and engines[alternative.engine_id].fact("eligible")
                        is AvailabilityState.TRUE
                        and engines[alternative.engine_id].executable_path is not None
                    ),
                    None,
                )
                evidence = (
                    f"observation:{observation.observation_id}@{observation.revision}",
                    *(f"ecosystem:{item}" for item in observed_ecosystems),
                )
                if chosen is None:
                    candidates.append(
                        EngineeringCommandCandidate(
                            pack_id=pack.pack_id,
                            pack_revision=pack.revision,
                            action=command.action,
                            state=EngineeringCommandState.UNAVAILABLE,
                            mode=command.mode,
                            declared_effects=command.declared_effects,
                            network=command.network,
                            evidence=(*evidence, "compatible-engine:unavailable"),
                        )
                    )
                    continue
                alternative, engine = chosen
                candidates.append(
                    EngineeringCommandCandidate(
                        pack_id=pack.pack_id,
                        pack_revision=pack.revision,
                        action=command.action,
                        state=EngineeringCommandState.READY,
                        engine_id=engine.engine_id,
                        executable=engine.executable_path,
                        arguments=alternative.arguments,
                        mode=command.mode,
                        declared_effects=command.declared_effects,
                        network=command.network,
                        evidence=(*evidence, f"engine:{engine.engine_id}:eligible"),
                    )
                )
        return tuple(candidates)

    def plan(
        self,
        observation: EnvironmentObservation,
        request: EngineeringCommandRequest,
    ) -> EngineeringCommandPlan:
        if (
            request.observation_id != observation.observation_id
            or request.observation_fingerprint != observation.fingerprint
        ):
            raise MishkanError(ErrorCode.REVISION_MISMATCH, "engineering command context is stale")
        candidate = next(
            (
                item
                for item in self.candidates(observation)
                if item.pack_id == request.pack_id and item.action == request.action
            ),
            None,
        )
        if candidate is None:
            raise MishkanError(ErrorCode.ENGINEERING, "engineering pack command is not applicable")
        if candidate.state is not EngineeringCommandState.READY:
            raise MishkanError(
                ErrorCode.TOOL_UNAVAILABLE,
                "engineering pack command has no observed eligible engine",
            )
        assert candidate.executable is not None
        return EngineeringCommandPlan(
            request=request,
            candidate=candidate,
            observation_fingerprint=observation.fingerprint,
            execution=ExecutionRequest(
                execution_id=request.request_id,
                mode=candidate.mode,
                executable=str(candidate.executable),
                args=candidate.arguments,
                cwd=".",
                environment={},
                credential_environment={},
                credential_references=(),
                owner=request.owner_identity,
                run_id=request.run_id,
                task_id=request.task_id,
                declared_executables=(str(candidate.executable),),
                session_profile=request.session_profile,
                deadline=request.deadline,
                timeout_seconds=request.timeout_seconds,
                expected_exit_codes=request.expected_exit_codes,
                network_destinations=(),
                declared_effects=candidate.declared_effects,
                # The plan carries no authority. session.start replaces this
                # placeholder with the fingerprint produced by policy evaluation.
                policy_fingerprint="0" * 64,
            ),
        )
