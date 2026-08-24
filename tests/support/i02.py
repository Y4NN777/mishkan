from __future__ import annotations

from pathlib import Path
from typing import Any

from mishkan.planning import PlanValidator
from mishkan.policy import (
    Decision,
    EffectivePolicy,
    PolicyAuthority,
    PolicyDocument,
    PolicyLoader,
    PolicyRule,
    PolicyScope,
)
from mishkan.policy.models import ResourceRequest, canonical_fingerprint
from mishkan.tools.catalog import ToolCatalog
from mishkan.tools.gateway_models import AdapterResult, InvocationContext
from mishkan.tools.inspection import ContentInspector, InspectionProfileLoader

CATALOG_URI = "package://mishkan.resources.tools/i02-catalog.yaml"
POLICY_URI = "package://mishkan.resources.policies/i02-local.yaml"
INSPECTION_URI = "package://mishkan.resources.inspection/i02-default.yaml"


class RecordingAdapter:
    def __init__(self, result: AdapterResult | BaseException) -> None:
        self.result = result
        self.calls = 0
        self.last_credentials: dict[str, str] | None = None

    def invoke(self, call: Any) -> AdapterResult:
        self.calls += 1
        self.last_credentials = dict(call.credentials)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def policy_for(
    capability: str,
    decision: Decision,
    *,
    effect_class: str,
    paths: tuple[str, ...] = ("*",),
    executables: tuple[str, ...] = ("*",),
    network_destinations: tuple[str, ...] = ("*",),
    remotes: tuple[str, ...] = ("*",),
    branches: tuple[str, ...] = ("*",),
    environments: tuple[str, ...] = ("*",),
    credentials: tuple[str, ...] = ("*",),
    external_resources: tuple[str, ...] = ("*",),
    allow_network: bool = False,
) -> EffectivePolicy:
    document = PolicyDocument(
        source_id="test.policy",
        revision="1",
        adoption_authority="Test Engineer",
        rules=(
            PolicyRule(
                rule_id="test.exact-capability",
                priority=100,
                decision=decision,
                scope=PolicyScope(
                    identities=("role:Engineer",),
                    objective_classes=("test",),
                    repositories=("test-repository",),
                    outcomes=("test.outcome",),
                    roles=("Engineer",),
                    capabilities=(capability,),
                    effect_classes=(effect_class,),
                    paths=paths,
                    executables=executables,
                    network_destinations=network_destinations,
                    remotes=remotes,
                    branches=branches,
                    environments=environments,
                    credentials=credentials,
                    external_resources=external_resources,
                    isolation_profiles=("*",),
                    max_timeout_seconds=60,
                    max_memory_mb=512,
                    allow_network=allow_network,
                    max_concurrency=1,
                ),
            ),
        ),
    )
    source = "test:policy"
    return EffectivePolicy(
        documents=(document,),
        source_uris=(source,),
        fingerprint=canonical_fingerprint({"source": source, "fingerprint": document.fingerprint}),
    )


def context_for(
    root: Path,
    tool_id: str,
    policy: EffectivePolicy,
    allowed_targets: tuple[str, ...],
    *,
    runtime: str = "python",
    network: bool = False,
    isolation_profile: str | None = None,
) -> InvocationContext:
    catalog = ToolCatalog((CATALOG_URI,), root, runtime=runtime)
    snapshot = catalog.snapshot((tool_id,))
    binding = catalog.bind(
        snapshot,
        task_id="task",
        role="Engineer",
        tool_id=tool_id,
        allowed_targets=allowed_targets,
    )
    return InvocationContext(
        run_id="run-1",
        task_attempt_id="task:1",
        identity="role:Engineer",
        objective_class="test",
        repository="test-repository",
        outcome="test.outcome",
        role="Engineer",
        plan_fingerprint="a" * 64,
        registry=snapshot,
        binding=binding,
        policy=policy,
        resources=ResourceRequest(
            timeout_seconds=30,
            memory_mb=512,
            network=network,
        ),
        isolation_profile=isolation_profile,
    )


def inspector(root: Path) -> ContentInspector:
    profile = InspectionProfileLoader().load(INSPECTION_URI, root)
    return ContentInspector(profile)


def plan_validator(root: Path) -> PlanValidator:
    catalog = ToolCatalog((CATALOG_URI,), root)
    policy = PolicyLoader().load((POLICY_URI,), root)
    return PlanValidator(catalog, policy, PolicyAuthority())
