from __future__ import annotations

from pathlib import Path

import pytest
from support.capabilities import context_for, inspector, policy_for

from mishkan.artifacts import FilesystemArtifactStore
from mishkan.policy import Decision, PolicyAuthority
from mishkan.tools.adapters import ContainerCommandAdapter
from mishkan.tools.gateway import CapabilityGateway, MappingCredentialResolver, MemoryEvidenceSink
from mishkan.tools.gateway_models import CallStatus, DeclaredTargets, InvocationContext
from mishkan.tools.isolation import IsolationProfileLoader, observe_container_commands


def _live_gateway(root: Path) -> tuple[CapabilityGateway, InvocationContext]:
    profile = IsolationProfileLoader().load(
        "package://mishkan.resources.isolation/local-no-network.yaml",
        root,
    )
    commands = observe_container_commands((profile,))
    if profile.profile_id not in commands:
        pytest.skip("configured container runtime or image is not ready")
    policy = policy_for(
        "command.run",
        Decision.ALLOW,
        effect_class="command",
        paths=(".",),
        executables=("python",),
        external_resources=("filesystem.write",),
    )
    context = context_for(
        root,
        "command.run",
        policy,
        (".", "python", "filesystem.write"),
        runtime="container",
        isolation_profile=profile.profile_id,
    )
    gateway = CapabilityGateway(
        root,
        PolicyAuthority(),
        MappingCredentialResolver({}),
        inspector(root),
        {"isolation.command": ContainerCommandAdapter(commands)},
        MemoryEvidenceSink(),
        artifact_store=FilesystemArtifactStore(
            root / ".mishkan" / "artifacts",
            max_artifact_bytes=16_384,
        ),
    )
    return gateway, context


@pytest.mark.container
@pytest.mark.commands
def test_live_isolated_command_contains_and_verifies_workspace_effects(tmp_path: Path) -> None:
    gateway, context = _live_gateway(tmp_path)
    targets = DeclaredTargets(
        paths=(".",),
        executables=("python",),
        external_resources=("filesystem.write",),
    )

    contained = gateway.invoke(
        context,
        {
            "argv": [
                "python",
                "-c",
                "from pathlib import Path;Path('generated.txt').write_text('contained')",
            ],
            "workspace": ".",
            "isolation_profile": "local.no-network",
            "declared_effects": ["filesystem.write"],
        },
        targets,
    )

    assert contained.status is CallStatus.COMPLETED
    assert contained.output is not None
    assert contained.output["changed_paths"] == ["generated.txt"]
    assert contained.output["effect_settlement"] == "completed"
    assert (tmp_path / "generated.txt").read_text(encoding="utf-8") == "contained"

    escaped = gateway.invoke(
        context,
        {
            "argv": [
                "python",
                "-c",
                "from pathlib import Path;Path('../escaped.txt').write_text('blocked')",
            ],
            "workspace": ".",
            "isolation_profile": "local.no-network",
            "declared_effects": ["filesystem.write"],
        },
        targets,
    )

    assert escaped.status is CallStatus.FAILED
    assert escaped.output is not None
    assert escaped.output["exit_code"] != 0
    assert not (tmp_path.parent / "escaped.txt").exists()
