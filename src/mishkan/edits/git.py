"""Separate governed adapters for explicit Git stateful effects."""

from __future__ import annotations

import subprocess
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mishkan.artifacts import ArtifactProvenance
from mishkan.artifacts.service import DurableArtifactService
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.policy import ApprovalEvidence, AuthorizationRequest, Decision, PolicyAuthority
from mishkan.policy.models import EffectivePolicy, ResourceRequest
from mishkan.tools.execution import EffectSettlement


class GitEffectMode(StrEnum):
    STAGE = "stage"
    COMMIT = "commit"
    PUSH = "push"
    FORCE_WITH_LEASE = "force_with_lease"
    FORCE_PUSH = "force_push"


class GitEffectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: GitEffectMode
    workspace: Path
    paths: tuple[str, ...] = ()
    remote: str | None = None
    branch: str | None = None
    author_name: str | None = None
    author_email: str | None = None
    message: str | None = None
    expected_remote_oid: str | None = None
    timeout_seconds: int = Field(default=120, ge=1, le=3600)

    @model_validator(mode="after")
    def mode_fields_are_exact(self) -> GitEffectRequest:
        if self.mode is GitEffectMode.STAGE and not self.paths:
            raise ValueError("Git stage requires explicit paths")
        if self.mode is GitEffectMode.COMMIT and not all(
            (self.author_name, self.author_email, self.message)
        ):
            raise ValueError("Git commit requires explicit author and message")
        if self.mode in {
            GitEffectMode.PUSH,
            GitEffectMode.FORCE_WITH_LEASE,
            GitEffectMode.FORCE_PUSH,
        } and not all((self.remote, self.branch)):
            raise ValueError("Git push effect requires explicit remote and branch")
        if self.mode is GitEffectMode.FORCE_WITH_LEASE and not self.expected_remote_oid:
            raise ValueError("force-with-lease requires the expected remote object id")
        return self


class GitEffectResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: GitEffectMode
    returncode: int
    settlement: EffectSettlement
    before_revision: str | None
    after_revision: str | None
    changed_paths: tuple[str, ...]
    diff_reference: str
    stdout: str
    stderr: str


class GovernedGitService:
    def __init__(self, artifacts: DurableArtifactService) -> None:
        self._artifacts = artifacts

    def execute(
        self,
        request: GitEffectRequest,
        *,
        authorization: AuthorizationRequest,
        policy: EffectivePolicy,
        approval: ApprovalEvidence | None = None,
    ) -> GitEffectResult:
        expected_effect = f"git.{request.mode.value}"
        if authorization.effects != (expected_effect,):
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "Git effect differs from the exact policy request",
            )
        decision = PolicyAuthority().evaluate(authorization, policy, approval)
        if decision.decision is not Decision.ALLOW:
            raise MishkanError(ErrorCode.AUTHORITY_NOT_GRANTED, "Git effect is not authorized")
        workspace = request.workspace.resolve(strict=True)
        before = self._run(workspace, ["rev-parse", "HEAD"], timeout=10)
        before_revision = before.stdout.strip() if before.returncode == 0 else None
        before_status = self._run(workspace, ["status", "--porcelain=v1"], timeout=10).stdout
        argv = self._argv(request)
        completed = self._run(workspace, argv, timeout=request.timeout_seconds)
        after = self._run(workspace, ["rev-parse", "HEAD"], timeout=10)
        after_revision = after.stdout.strip() if after.returncode == 0 else None
        after_status = self._run(workspace, ["status", "--porcelain=v1"], timeout=10).stdout
        diff = self._run(workspace, ["diff", "--binary", "HEAD"], timeout=30)
        changed_paths = tuple(
            sorted(
                {
                    line[3:]
                    for line in (*before_status.splitlines(), *after_status.splitlines())
                    if len(line) > 3
                }
            )
        )
        manifest = self._artifacts.put_bytes(
            diff.stdout.encode(),
            media_type="text/x-diff",
            provenance=ArtifactProvenance(
                producer_identity=authorization.identity,
                run_id=authorization.plan_fingerprint,
                task_attempt_id="git-effect",
                call_id=authorization.fingerprint,
                capability=f"git.{request.mode.value}",
                channel="diff",
            ),
            complete=True,
            retention="git-effect",
        )
        remote = request.mode in {
            GitEffectMode.PUSH,
            GitEffectMode.FORCE_WITH_LEASE,
            GitEffectMode.FORCE_PUSH,
        }
        settlement = (
            EffectSettlement.COMPLETED
            if completed.returncode == 0
            else EffectSettlement.UNCERTAIN
            if remote
            else EffectSettlement.ABSENT
        )
        return GitEffectResult(
            mode=request.mode,
            returncode=completed.returncode,
            settlement=settlement,
            before_revision=before_revision,
            after_revision=after_revision,
            changed_paths=changed_paths,
            diff_reference=manifest.reference,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    @staticmethod
    def authorization_request(
        request: GitEffectRequest,
        *,
        plan_fingerprint: str,
        identity: str,
        repository: str,
        role: str,
    ) -> AuthorizationRequest:
        return AuthorizationRequest(
            plan_fingerprint=plan_fingerprint,
            identity=identity,
            objective_class="git-effect",
            repository=repository,
            outcome="git-effect",
            role=role,
            capability=f"git.{request.mode.value}",
            effect_class="stateful",
            effects=(f"git.{request.mode.value}",),
            paths=request.paths,
            remotes=(request.remote,) if request.remote else (),
            branches=(request.branch,) if request.branch else (),
            resources=ResourceRequest(timeout_seconds=request.timeout_seconds, network=True),
        )

    @staticmethod
    def _argv(request: GitEffectRequest) -> list[str]:
        if request.mode is GitEffectMode.STAGE:
            return ["add", "--", *request.paths]
        if request.mode is GitEffectMode.COMMIT:
            assert request.author_name and request.author_email and request.message
            return [
                "-c",
                f"user.name={request.author_name}",
                "-c",
                f"user.email={request.author_email}",
                "commit",
                "-m",
                request.message,
            ]
        assert request.remote and request.branch
        if request.mode is GitEffectMode.PUSH:
            return ["push", request.remote, request.branch]
        if request.mode is GitEffectMode.FORCE_WITH_LEASE:
            assert request.expected_remote_oid
            return [
                "push",
                f"--force-with-lease={request.branch}:{request.expected_remote_oid}",
                request.remote,
                request.branch,
            ]
        return ["push", "--force", request.remote, request.branch]

    @staticmethod
    def _run(
        workspace: Path, arguments: list[str], *, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
        )
