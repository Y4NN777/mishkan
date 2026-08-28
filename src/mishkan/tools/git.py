"""Gateway adapters for the five separately governed Git effects."""

from __future__ import annotations

from pathlib import Path

from mishkan.edits.git import GitEffectMode, GitEffectRequest, GovernedGitService
from mishkan.policy import AuthorizationRequest
from mishkan.policy.models import EffectivePolicy, canonical_fingerprint
from mishkan.tools.adapters import AdapterCall, CapabilityAdapter
from mishkan.tools.execution import EffectSettlement
from mishkan.tools.gateway_models import AdapterResult, CallStatus


class GovernedGitAdapter:
    def __init__(
        self,
        mode: GitEffectMode,
        workspace: Path,
        service: GovernedGitService,
        policy: EffectivePolicy,
    ) -> None:
        self.adapter_id = f"native.git.{mode.value}"
        self._mode = mode
        self._workspace = workspace.resolve(strict=True)
        self._service = service
        self._policy = policy

    def invoke(self, call: AdapterCall) -> AdapterResult:
        if call.capability != f"git.{self._mode.value}":
            raise ValueError("Git adapter capability differs from its fixed effect mode")
        request_payload = {"mode": self._mode.value, "workspace": self._workspace, **call.arguments}
        credential_value: str | None = None
        if call.credentials:
            if set(call.credentials) != {"git.remote"}:
                raise ValueError("Git adapter received an unexpected credential reference")
            request_payload["credential_reference"] = "git.remote"
            credential_value = call.credentials["git.remote"]
        request = GitEffectRequest.model_validate(request_payload)
        plan_fingerprint = call.plan_fingerprint or canonical_fingerprint(
            {
                "run_id": call.run_id,
                "task_attempt_id": call.task_attempt_id,
                "execution_id": call.execution_id,
            }
        )
        authorization = AuthorizationRequest(
            plan_fingerprint=plan_fingerprint,
            identity=call.acting_identity,
            objective_class=call.objective_class,
            repository=call.repository or str(self._workspace),
            outcome=call.outcome,
            role=call.role,
            capability=call.capability,
            effect_class=self._mode.effect_class,
            effects=(call.capability,),
            paths=tuple(item.relative for item in call.targets.paths),
            remotes=call.targets.remotes,
            branches=call.targets.branches,
            network_destinations=call.targets.network_destinations,
            credentials=(request.credential_reference,) if request.credential_reference else (),
            resources=call.resources,
        )
        result = self._service.execute(
            request,
            authorization=authorization,
            policy=self._policy,
            credential_value=credential_value,
        )
        status = (
            CallStatus.COMPLETED
            if result.settlement is EffectSettlement.COMPLETED
            else CallStatus.UNCERTAIN
            if result.settlement is EffectSettlement.UNCERTAIN
            else CallStatus.FAILED
        )
        return AdapterResult(
            output=result.model_dump(mode="json"),
            actual_targets=call.targets,
            evidence={
                "repository_root": result.repository_root,
                "git_directory": result.git_directory,
                "validation": list(result.validation),
                "diff_reference": result.diff_reference,
            },
            inspection_content=(result.stdout, result.stderr),
            call_status=status,
            retryable=False,
            reason=None if result.returncode == 0 else "Git effect did not settle successfully",
        )


def build_git_adapters(
    workspace: Path,
    service: GovernedGitService,
    policy: EffectivePolicy,
) -> dict[str, CapabilityAdapter]:
    return {
        f"native.git.{mode.value}": GovernedGitAdapter(mode, workspace, service, policy)
        for mode in GitEffectMode
    }
