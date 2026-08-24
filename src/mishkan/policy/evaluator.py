"""Deterministic closed-world policy authorization."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.policy.models import (
    ApprovalEvidence,
    AuthorizationDecision,
    AuthorizationRequest,
    Decision,
    EffectivePolicy,
    PolicyDocument,
    PolicyRule,
    PolicyScope,
)


@dataclass(frozen=True, slots=True)
class _Match:
    document: PolicyDocument
    rule: PolicyRule
    score: tuple[int, int, int]


class PolicyAuthority:
    def evaluate(
        self,
        request: AuthorizationRequest,
        policy: EffectivePolicy,
        approval: ApprovalEvidence | None = None,
    ) -> AuthorizationDecision:
        matches = [
            _Match(
                document, rule, (document.priority, rule.priority, self._specificity(rule.scope))
            )
            for document in policy.documents
            for rule in document.rules
            if self._matches(request, rule.scope)
        ]
        revisions = tuple(f"{item.source_id}@{item.revision}" for item in policy.documents)
        if not matches:
            return AuthorizationDecision(
                request_fingerprint=request.fingerprint,
                plan_fingerprint=request.plan_fingerprint,
                policy_fingerprint=policy.fingerprint,
                policy_revisions=revisions,
                decision=Decision.DENY,
                matched_rule_ids=(),
                decided_by="closed-world-default",
                reason="no public policy rule grants the exact requested scope",
            )

        winning_score = max(match.score for match in matches)
        winners = [match for match in matches if match.score == winning_score]
        decisions = {match.rule.decision for match in winners}
        if len(decisions) != 1:
            raise MishkanError(
                ErrorCode.POLICY_CONFLICT,
                "equally ranked policy rules conflict",
                details={
                    "request_fingerprint": request.fingerprint,
                    "rules": sorted(match.rule.rule_id for match in winners),
                    "decisions": sorted(decision.value for decision in decisions),
                },
            )

        winner = sorted(winners, key=lambda item: item.rule.rule_id)[0]
        decision = winner.rule.decision
        approval_id: str | None = None
        decided_by = f"policy:{winner.document.source_id}:{winner.rule.rule_id}"
        reason = f"matched public policy rule {winner.rule.rule_id}"
        if decision is Decision.REQUIRE_APPROVAL and approval is not None:
            if not approval.is_active_for(request, policy):
                raise MishkanError(
                    ErrorCode.AUTHORIZATION_MISSING,
                    "approval does not match the exact request and policy",
                    details={"request_fingerprint": request.fingerprint},
                )
            decision = Decision.ALLOW
            approval_id = str(approval.id)
            decided_by = approval.approved_by
            reason = f"interactive approval satisfied rule {winner.rule.rule_id}"

        return AuthorizationDecision(
            request_fingerprint=request.fingerprint,
            plan_fingerprint=request.plan_fingerprint,
            policy_fingerprint=policy.fingerprint,
            policy_revisions=revisions,
            decision=decision,
            matched_rule_ids=tuple(sorted(match.rule.rule_id for match in winners)),
            matched_scope=winner.rule.scope,
            decided_by=decided_by,
            approval_id=approval_id,
            reason=reason,
        )

    @classmethod
    def _matches(cls, request: AuthorizationRequest, scope: PolicyScope) -> bool:
        scalar_pairs = (
            (request.identity, scope.identities),
            (request.objective_class, scope.objective_classes),
            (request.repository, scope.repositories),
            (request.outcome, scope.outcomes),
            (request.role, scope.roles),
            (request.capability, scope.capabilities),
            (request.effect_class, scope.effect_classes),
        )
        if not all(cls._matches_one(value, selectors) for value, selectors in scalar_pairs):
            return False
        collection_pairs = (
            (request.paths, scope.paths),
            (request.executables, scope.executables),
            (request.network_destinations, scope.network_destinations),
            (request.remotes, scope.remotes),
            (request.branches, scope.branches),
            (request.environments, scope.environments),
            (request.credentials, scope.credentials),
            (request.external_resources, scope.external_resources),
        )
        if not all(cls._matches_all(values, selectors) for values, selectors in collection_pairs):
            return False
        if request.isolation_profile is not None and not cls._matches_one(
            request.isolation_profile, scope.isolation_profiles
        ):
            return False
        resources = request.resources
        return not (
            (
                scope.max_timeout_seconds is not None
                and resources.timeout_seconds > scope.max_timeout_seconds
            )
            or (
                scope.max_memory_mb is not None and (resources.memory_mb or 0) > scope.max_memory_mb
            )
            or (scope.allow_network is not None and resources.network is not scope.allow_network)
            or (scope.max_concurrency is not None and resources.concurrency > scope.max_concurrency)
        )

    @staticmethod
    def _matches_one(value: str, selectors: tuple[str, ...]) -> bool:
        return any(fnmatchcase(value, selector) for selector in selectors)

    @classmethod
    def _matches_all(cls, values: tuple[str, ...], selectors: tuple[str, ...]) -> bool:
        return all(cls._matches_one(value, selectors) for value in values)

    @staticmethod
    def _specificity(scope: PolicyScope) -> int:
        selector_groups = (
            scope.identities,
            scope.objective_classes,
            scope.repositories,
            scope.outcomes,
            scope.roles,
            scope.capabilities,
            scope.effect_classes,
            scope.paths,
            scope.executables,
            scope.network_destinations,
            scope.remotes,
            scope.branches,
            scope.environments,
            scope.credentials,
            scope.external_resources,
            scope.isolation_profiles,
        )
        selector_score = sum(
            0
            if selectors == ("*",)
            else 1 + sum("*" not in item and "?" not in item for item in selectors)
            for selectors in selector_groups
        )
        constraint_score = sum(
            value is not None
            for value in (
                scope.max_timeout_seconds,
                scope.max_memory_mb,
                scope.allow_network,
                scope.max_concurrency,
            )
        )
        return selector_score + constraint_score
