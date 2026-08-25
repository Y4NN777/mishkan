from __future__ import annotations

from pathlib import Path

from support.i02 import context_for, inspector, policy_for

from mishkan.policy import Decision, PolicyAuthority
from mishkan.tools.adapters import SearchFilesAdapter
from mishkan.tools.gateway import CapabilityGateway, MappingCredentialResolver, MemoryEvidenceSink
from mishkan.tools.gateway_models import CallStatus, DeclaredTargets


def gateway_for(root: Path, adapter: SearchFilesAdapter) -> CapabilityGateway:
    return CapabilityGateway(
        root,
        PolicyAuthority(),
        MappingCredentialResolver({}),
        inspector(root),
        {adapter.adapter_id: adapter},
        MemoryEvidenceSink(),
    )


def test_search_files_finds_only_requested_types_and_patterns(tmp_path: Path) -> None:
    (tmp_path / "project" / "src").mkdir(parents=True)
    (tmp_path / "project" / "src" / "main.py").touch()
    (tmp_path / "project" / "src" / "notes.txt").touch()
    (tmp_path / "project" / "src" / ".hidden.py").touch()
    (tmp_path / "project" / "tests").mkdir()
    (tmp_path / "project" / "tests" / "test_main.py").touch()
    policy = policy_for("search.files", Decision.ALLOW, effect_class="read", paths=("project",))
    context = context_for(tmp_path, "search.files", policy, ("project",))
    adapter = SearchFilesAdapter(max_results=20, max_traversal_entries=100)

    result = gateway_for(tmp_path, adapter).invoke(
        context,
        {
            "path": "project",
            "patterns": ["*.py"],
            "exclude": ["tests/*"],
            "object_types": ["file"],
            "include_hidden": False,
            "max_depth": 4,
        },
        DeclaredTargets(paths=("project",)),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert [match["path"] for match in result.output["matches"]] == ["src/main.py"]
    assert result.output["engine"] == "python.scandir"
    assert result.output["truncated"] is False


def test_search_files_pagination_refuses_a_changed_view(tmp_path: Path) -> None:
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "a.py").touch()
    (tmp_path / "project" / "b.py").touch()
    policy = policy_for("search.files", Decision.ALLOW, effect_class="read", paths=("project",))
    context = context_for(tmp_path, "search.files", policy, ("project",))
    adapter = SearchFilesAdapter(max_results=1, max_traversal_entries=100)
    gateway = gateway_for(tmp_path, adapter)
    arguments = {"path": "project", "patterns": ["*.py"], "max_results": 1}

    first = gateway.invoke(context, arguments, DeclaredTargets(paths=("project",)))

    assert first.status is CallStatus.COMPLETED
    assert first.output is not None
    assert first.output["truncated"] is True
    cursor = first.output["continuation_cursor"]
    (tmp_path / "project" / "aa.py").touch()
    changed = gateway.invoke(
        context,
        {**arguments, "cursor": cursor},
        DeclaredTargets(paths=("project",)),
    )

    assert changed.status is CallStatus.FAILED
    assert changed.error_code == "ERR-FIL-001"
