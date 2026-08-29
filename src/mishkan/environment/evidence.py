"""Derive truthful environment attempts and verification from I03 execution evidence."""

from __future__ import annotations

import hashlib
import json

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.environment.models import (
    AvailabilityState,
    EnvironmentAttempt,
    EnvironmentBindingState,
    EnvironmentOperation,
    EnvironmentOperationPlan,
    EnvironmentSettlement,
    EnvironmentVerification,
    EnvironmentVerificationRequest,
)
from mishkan.environment.profile import EnvironmentProfile
from mishkan.environment.repository import SQLiteEnvironmentRepository
from mishkan.execution import ExecutionSession
from mishkan.execution.sessions import SessionState
from mishkan.tools.execution import EffectSettlement, ExecutionStatus


class EnvironmentEvidenceService:
    def __init__(
        self,
        profile: EnvironmentProfile,
        repository: SQLiteEnvironmentRepository,
    ) -> None:
        self._repository = repository
        self._adapter_engines = {
            adapter.adapter_id: adapter.engine_id for adapter in profile.adapters
        }

    def settle_attempt(
        self,
        plan: EnvironmentOperationPlan,
        session: ExecutionSession,
    ) -> EnvironmentAttempt:
        binding = self._repository.binding(str(plan.request.binding_id))
        if binding.state is not EnvironmentBindingState.COMPATIBLE:
            raise MishkanError(
                ErrorCode.ENGINEERING,
                "environment attempt cannot settle against an invalid binding",
            )
        observation = self._repository.observation(str(binding.request.observation_id))
        if (
            binding.revision != plan.binding_revision
            or observation.fingerprint != plan.observation_fingerprint
        ):
            raise MishkanError(
                ErrorCode.REVISION_MISMATCH,
                "environment operation plan is stale",
            )
        result = session.result
        if result is None or session.state not in {
            SessionState.SETTLED,
            SessionState.FAILED,
            SessionState.LOST,
            SessionState.UNCERTAIN,
        }:
            raise MishkanError(
                ErrorCode.EXECUTION,
                "environment attempt requires a terminal execution result",
            )
        expected = plan.execution
        if (
            session.execution_id != expected.execution_id
            or session.owner != plan.request.owner_identity
            or session.run_id != plan.request.run_id
            or session.task_id != plan.request.task_id
            or result.executable != expected.executable
            or result.args != expected.args
            or result.cwd != expected.cwd
            or result.declared_effects != expected.declared_effects
        ):
            raise MishkanError(
                ErrorCode.REVISION_MISMATCH,
                "environment execution result differs from its exact operation plan",
            )
        settlement = self._settlement(session)
        references = tuple(
            dict.fromkeys(
                (
                    *((result.stdout_artifact_ref,) if result.stdout_artifact_ref else ()),
                    *((result.stderr_artifact_ref,) if result.stderr_artifact_ref else ()),
                    *result.produced_artifact_refs,
                    *(
                        (result.effect_diff_artifact_ref,)
                        if result.effect_diff_artifact_ref
                        else ()
                    ),
                )
            )
        )
        engine_id = self._adapter_engines.get(plan.request.adapter_id)
        if engine_id is None:
            raise MishkanError(ErrorCode.TOOL_UNAVAILABLE, "environment adapter engine is unknown")
        engine = next(item for item in observation.engines if item.engine_id == engine_id)
        attempt = EnvironmentAttempt(
            binding_id=binding.binding_id,
            operation_plan_fingerprint=plan.fingerprint,
            operation=plan.request.operation,
            adapter_id=plan.request.adapter_id,
            engine_id=engine_id,
            engine_version=engine.version,
            execution_location=observation.execution_location,
            effect_call_ids=(str(session.execution_id),),
            artifact_references=references,
            execution_status=result.status,
            effect_settlement=result.effect_settlement,
            declared_effects=result.declared_effects,
            observed_effects=result.observed_effects,
            exit_code=result.exit_code,
            settlement=settlement,
            started_at=result.started_at,
            completed_at=result.finished_at,
            limitations=(
                ("external effect requires an explicit read-only reconciliation probe",)
                if settlement is EnvironmentSettlement.UNCERTAIN and result.declared_effects
                else ()
            ),
        )
        return self._repository.record_attempt(attempt)

    def verify(self, request: EnvironmentVerificationRequest) -> EnvironmentVerification:
        binding = self._repository.binding(str(request.binding_id))
        if binding.state is not EnvironmentBindingState.COMPATIBLE:
            raise MishkanError(
                ErrorCode.ENGINEERING,
                "environment verification requires a compatible binding",
            )
        if binding.request.owner_identity != request.owner_identity:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "environment verification owner differs from the binding owner",
            )
        observation = self._repository.observation(str(binding.request.observation_id))
        if request.context_fingerprint != observation.fingerprint:
            raise MishkanError(
                ErrorCode.REVISION_MISMATCH,
                "environment verification context is stale",
            )
        required = set(binding.request.verification_checks)
        missing = sorted(required - set(request.check_attempt_ids))
        if missing:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "environment verification omits required checks",
                details={"missing": tuple(missing)},
            )
        attempts_by_check = {
            name: tuple(self._repository.attempt(str(identity)) for identity in identities)
            for name, identities in request.check_attempt_ids.items()
        }
        unique_attempts = {
            attempt.attempt_id: attempt
            for values in attempts_by_check.values()
            for attempt in values
        }
        attempts = tuple(unique_attempts.values())
        if any(
            attempt.binding_id != request.binding_id
            or attempt.engine_id != request.engine_id
            or attempt.execution_location != observation.execution_location
            for attempt in attempts
        ):
            raise MishkanError(
                ErrorCode.REVISION_MISMATCH,
                "environment verification combines incompatible attempt evidence",
            )
        checks = {name: self._check_state(values) for name, values in attempts_by_check.items()}
        verified = all(value is AvailabilityState.TRUE for value in checks.values())
        artifacts = tuple(
            dict.fromkeys(
                reference for attempt in attempts for reference in attempt.artifact_references
            )
        )
        versions = {attempt.engine_version for attempt in attempts}
        inconsistent_versions = len(versions) > 1
        engine_version = next(iter(versions)) if len(versions) == 1 else None
        limitations = tuple(
            dict.fromkeys(
                (
                    *request.limitations,
                    *(item for attempt in attempts for item in attempt.limitations),
                    *(
                        ("engine version evidence is inconsistent",)
                        if inconsistent_versions
                        else ()
                    ),
                    *(("engine version is unknown",) if engine_version is None else ()),
                )
            )
        )
        verification = EnvironmentVerification(
            verification_id=request.verification_id,
            binding_id=request.binding_id,
            context_fingerprint=observation.fingerprint,
            location_fingerprint=self._location_fingerprint(
                observation.execution_location,
                observation.platform,
                observation.architecture,
                request.engine_id,
                engine_version,
            ),
            engine_id=request.engine_id,
            engine_version=engine_version,
            checks=checks,
            attempt_ids=tuple(attempt.attempt_id for attempt in attempts),
            artifact_references=artifacts,
            settlement=(
                EnvironmentSettlement.VERIFIED
                if verified
                else EnvironmentSettlement.FAILED
                if any(value is AvailabilityState.FALSE for value in checks.values())
                else EnvironmentSettlement.UNCERTAIN
            ),
            limitations=limitations,
        )
        return self._repository.record_verification(verification)

    @staticmethod
    def _settlement(session: ExecutionSession) -> EnvironmentSettlement:
        assert session.result is not None
        result = session.result
        if (
            session.state in {SessionState.LOST, SessionState.UNCERTAIN}
            or result.effect_settlement is EffectSettlement.UNCERTAIN
        ):
            return EnvironmentSettlement.UNCERTAIN
        if result.status is ExecutionStatus.CANCELLED:
            return EnvironmentSettlement.CANCELLED
        if result.status is ExecutionStatus.COMPLETED:
            return EnvironmentSettlement.VERIFIED
        return EnvironmentSettlement.FAILED

    @staticmethod
    def _check_state(attempts: tuple[EnvironmentAttempt, ...]) -> AvailabilityState:
        ordered = tuple(sorted(attempts, key=lambda item: item.completed_at))
        if any(
            item.settlement in {EnvironmentSettlement.FAILED, EnvironmentSettlement.CANCELLED}
            for item in ordered
        ):
            return AvailabilityState.FALSE
        if all(item.settlement is EnvironmentSettlement.VERIFIED for item in ordered):
            return AvailabilityState.TRUE
        final = ordered[-1]
        if (
            final.settlement is EnvironmentSettlement.VERIFIED
            and final.operation in {EnvironmentOperation.VALIDATE, EnvironmentOperation.READINESS}
            and not final.declared_effects
            and all(
                item.settlement in {EnvironmentSettlement.VERIFIED, EnvironmentSettlement.UNCERTAIN}
                for item in ordered[:-1]
            )
        ):
            return AvailabilityState.TRUE
        return AvailabilityState.UNKNOWN

    @staticmethod
    def _location_fingerprint(
        location: str,
        platform: str,
        architecture: str,
        engine_id: str,
        engine_version: str | None,
    ) -> str:
        payload = {
            "location": location,
            "platform": platform,
            "architecture": architecture,
            "engine_id": engine_id,
            "engine_version": engine_version,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
