"""Compatibility-only resolution of an agent-authored environment request."""

from __future__ import annotations

from datetime import timedelta

from mishkan.domain.time import utc_now
from mishkan.environment.models import (
    AvailabilityState,
    DescriptorObservation,
    EngineObservation,
    EnvironmentBinding,
    EnvironmentBindingRequest,
    EnvironmentBindingState,
    EnvironmentObservation,
    EnvironmentOutcome,
)


class EnvironmentResolver:
    def __init__(self, *, freshness_seconds: int) -> None:
        self._freshness_seconds = freshness_seconds

    def resolve(
        self,
        request: EnvironmentBindingRequest,
        observation: EnvironmentObservation,
    ) -> EnvironmentBinding:
        stale_reasons = self._stale_reasons(request, observation)
        if stale_reasons:
            return self._refuse(request, EnvironmentBindingState.STALE, stale_reasons)
        if request.requested_outcome is EnvironmentOutcome.UNRESOLVED:
            return self._refuse(
                request,
                EnvironmentBindingState.UNRESOLVED,
                ("plan requested an unresolved environment decision",),
            )
        platform_reasons = tuple(
            reason
            for condition, reason in (
                (
                    request.target_platform == observation.platform,
                    f"platform:{request.target_platform}",
                ),
                (
                    request.target_architecture == observation.architecture,
                    f"architecture:{request.target_architecture}",
                ),
                (
                    request.execution_location == observation.execution_location,
                    f"execution-location:{request.execution_location}",
                ),
            )
            if not condition
        )
        if platform_reasons:
            return self._refuse(request, EnvironmentBindingState.INCOMPATIBLE, platform_reasons)
        descriptors = tuple(
            item
            for item in observation.descriptors
            if not request.allowed_descriptor_formats
            or item.format in request.allowed_descriptor_formats
        )
        engines = tuple(
            engine for engine in observation.engines if self._engine_satisfies(engine, request)
        )
        if request.requested_outcome is EnvironmentOutcome.REUSE_EXISTING:
            if not descriptors:
                return self._refuse(
                    request,
                    EnvironmentBindingState.INCOMPATIBLE,
                    ("compatible-existing-descriptor",),
                )
            if (request.required_engine_ids or request.required_semantics) and not engines:
                return self._refuse(
                    request,
                    EnvironmentBindingState.INCOMPATIBLE,
                    self._missing_engine_conditions(request),
                )
            return self._compatible(request, engines, descriptors)
        if request.requested_outcome is EnvironmentOutcome.HOST_NATIVE:
            if not engines:
                return self._refuse(
                    request,
                    EnvironmentBindingState.INCOMPATIBLE,
                    self._missing_engine_conditions(request),
                )
            return self._compatible(request, engines, ())
        if request.requested_outcome in {
            EnvironmentOutcome.GENERATE,
            EnvironmentOutcome.PROPOSE_PROJECT_CHANGE,
        }:
            if not request.allowed_descriptor_formats:
                return self._refuse(
                    request,
                    EnvironmentBindingState.INCOMPATIBLE,
                    ("descriptor-format-selection-constraint",),
                )
            if not engines:
                return self._refuse(
                    request,
                    EnvironmentBindingState.INCOMPATIBLE,
                    self._missing_engine_conditions(request),
                )
            return self._compatible(request, engines, ())
        raise AssertionError("validated environment outcome is unhandled")

    def _stale_reasons(
        self,
        request: EnvironmentBindingRequest,
        observation: EnvironmentObservation,
    ) -> tuple[str, ...]:
        reasons = []
        if request.context_id != observation.context_id:
            reasons.append("context-identity")
        if request.observation_id != observation.observation_id:
            reasons.append("observation-identity")
        if request.observation_revision != observation.revision:
            reasons.append("observation-revision")
        if request.observation_fingerprint != observation.fingerprint:
            reasons.append("observation-fingerprint")
        if utc_now() - observation.observed_at > timedelta(seconds=self._freshness_seconds):
            reasons.append("observation-freshness")
        return tuple(reasons)

    @staticmethod
    def _engine_satisfies(
        engine: EngineObservation,
        request: EnvironmentBindingRequest,
    ) -> bool:
        if request.required_engine_ids and engine.engine_id not in request.required_engine_ids:
            return False
        if engine.engine_id not in request.authorized_engine_ids:
            return False
        if engine.fact("eligible") is not AvailabilityState.TRUE:
            return False
        return set(request.required_semantics).issubset(engine.semantics)

    @staticmethod
    def _missing_engine_conditions(request: EnvironmentBindingRequest) -> tuple[str, ...]:
        conditions = [f"engine:{value}" for value in request.required_engine_ids]
        conditions.extend(f"semantic:{value}" for value in request.required_semantics)
        conditions.append("authorized-compatible-engine")
        return tuple(dict.fromkeys(conditions))

    @staticmethod
    def _compatible(
        request: EnvironmentBindingRequest,
        engines: tuple[EngineObservation, ...],
        descriptors: tuple[DescriptorObservation, ...],
    ) -> EnvironmentBinding:
        return EnvironmentBinding(
            request=request,
            state=EnvironmentBindingState.COMPATIBLE,
            selected_engine_ids=tuple(item.engine_id for item in engines),
            selected_adapter_ids=tuple(
                item.adapter_id for item in engines if item.adapter_id is not None
            ),
            selected_descriptors=tuple(descriptors),
            missing_conditions=(),
            lost_fidelity=(),
            reason="exact requested environment outcome is compatible with current evidence",
        )

    @staticmethod
    def _refuse(
        request: EnvironmentBindingRequest,
        state: EnvironmentBindingState,
        reasons: tuple[str, ...],
    ) -> EnvironmentBinding:
        return EnvironmentBinding(
            request=request,
            state=state,
            selected_engine_ids=(),
            selected_adapter_ids=(),
            selected_descriptors=(),
            missing_conditions=reasons,
            lost_fidelity=(),
            reason="environment request cannot be satisfied without changing its declared outcome",
        )
