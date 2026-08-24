from __future__ import annotations

import json
from pathlib import Path

from crewai.tools import BaseTool
from support.i02 import context_for, inspector, policy_for

from mishkan.policy import ApprovalEvidence, AuthorizationRequest, Decision, PolicyAuthority
from mishkan.tools.adapters import ReadFileAdapter
from mishkan.tools.crewai_gateway import GatewayCrewAITool
from mishkan.tools.gateway import CapabilityGateway, MappingCredentialResolver, MemoryEvidenceSink
from mishkan.tools.gateway_models import DeclaredTargets


def test_exact_governed_binding_is_exposed_through_supported_crewai_tool(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("governed evidence", encoding="utf-8")
    policy = policy_for(
        "repository.read_file",
        Decision.ALLOW,
        effect_class="read",
        paths=("README.md",),
    )
    context = context_for(tmp_path, "repository.read_file", policy, ("README.md",))
    contract = context.registry.require("repository.read_file")
    gateway = CapabilityGateway(
        tmp_path,
        PolicyAuthority(),
        MappingCredentialResolver({}),
        inspector(tmp_path),
        {"native.repository.read_file": ReadFileAdapter(contract.max_bytes)},
        MemoryEvidenceSink(),
    )
    tool = GatewayCrewAITool(
        contract,
        gateway,
        context,
        lambda arguments: DeclaredTargets(paths=(str(arguments["path"]),)),
    )

    output = json.loads(tool.run(path="README.md"))

    assert isinstance(tool, BaseTool)
    assert tool.name == "repository_read_file"
    assert output == {"path": "README.md", "content": "governed evidence"}


def test_crewai_binding_selects_the_exact_accepted_approval(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("approved evidence", encoding="utf-8")
    policy = policy_for(
        "repository.read_file",
        Decision.REQUIRE_APPROVAL,
        effect_class="read",
        paths=("README.md",),
    )
    context = context_for(tmp_path, "repository.read_file", policy, ("README.md",))
    contract = context.registry.require("repository.read_file")
    request = AuthorizationRequest(
        plan_fingerprint=context.plan_fingerprint,
        identity=context.identity,
        objective_class=context.objective_class,
        repository=context.repository,
        outcome=context.outcome,
        role=context.role,
        capability=contract.tool_id,
        effect_class=contract.effect_class.value,
        paths=("README.md",),
        credentials=contract.credential_refs,
        resources=context.resources,
    )
    approval = ApprovalEvidence(
        request_fingerprint=request.fingerprint,
        plan_fingerprint=context.plan_fingerprint,
        policy_fingerprint=policy.fingerprint,
        approved_by="engineer:test",
        reason="Approve the exact read for this accepted plan.",
    )
    gateway = CapabilityGateway(
        tmp_path,
        PolicyAuthority(),
        MappingCredentialResolver({}),
        inspector(tmp_path),
        {"native.repository.read_file": ReadFileAdapter(contract.max_bytes)},
        MemoryEvidenceSink(),
    )
    tool = GatewayCrewAITool(
        contract,
        gateway,
        context,
        approval=(approval,),
    )

    output = json.loads(tool.run(path="README.md"))

    assert output["content"] == "approved evidence"
