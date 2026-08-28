from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from support.capabilities import context_for, inspector, policy_for

from mishkan.policy import Decision, PolicyAuthority
from mishkan.tools.adapters import (
    CapabilityAdapter,
    GitHistorySearchAdapter,
    PythonStructuralSearchAdapter,
    PythonSymbolSearchAdapter,
    RipgrepTextSearchAdapter,
    SearchFilesAdapter,
)
from mishkan.tools.gateway import CapabilityGateway, MappingCredentialResolver, MemoryEvidenceSink
from mishkan.tools.gateway_models import CallStatus, DeclaredTargets


def gateway_for(root: Path, adapter: CapabilityAdapter) -> CapabilityGateway:
    return CapabilityGateway(
        root,
        PolicyAuthority(),
        MappingCredentialResolver({}),
        inspector(root),
        {adapter.adapter_id: adapter},
        MemoryEvidenceSink(),
    )


def ripgrep_adapter(*, max_results: int) -> RipgrepTextSearchAdapter:
    executable = shutil.which("rg")
    if executable is None:
        pytest.skip("ripgrep is not installed")
    version = subprocess.run(
        [executable, "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()[0]
    return RipgrepTextSearchAdapter(
        Path(executable),
        version,
        max_results=max_results,
        max_output_bytes=64_000,
        timeout_seconds=5,
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


def test_structural_search_reports_syntax_coverage_without_semantic_claims(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "service.py").write_text(
        "class Service:\n    def run(self):\n        return build()\n",
        encoding="utf-8",
    )
    policy = policy_for(
        "search.structural", Decision.ALLOW, effect_class="read", paths=("project",)
    )
    context = context_for(tmp_path, "search.structural", policy, ("project",))
    adapter = PythonStructuralSearchAdapter(
        max_results=20,
        max_files=20,
        max_file_bytes=64_000,
    )

    result = gateway_for(tmp_path, adapter).invoke(
        context,
        {"path": "project", "node_types": ["ClassDef"], "name": "Service"},
        DeclaredTargets(paths=("project",)),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert result.output["matches"] == [
        {
            "path": "service.py",
            "node_type": "ClassDef",
            "name": "Service",
            "line": 1,
            "end_line": 3,
        }
    ]
    assert result.output["coverage"] == "syntax"
    assert result.output["semantic_coverage"] is False
    assert result.output["omissions"] == []


def test_symbol_search_distinguishes_syntax_definitions_and_references(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "symbols.py").write_text(
        "def build():\n    return 1\n\nresult = build()\n",
        encoding="utf-8",
    )
    policy = policy_for("search.symbol", Decision.ALLOW, effect_class="read", paths=("project",))
    context = context_for(tmp_path, "search.symbol", policy, ("project",))
    adapter = PythonSymbolSearchAdapter(max_results=20, max_files=20, max_file_bytes=64_000)

    result = gateway_for(tmp_path, adapter).invoke(
        context,
        {"path": "project", "identifier": "build", "relation": "both"},
        DeclaredTargets(paths=("project",)),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert {match["relation"] for match in result.output["matches"]} == {
        "definition",
        "reference",
    }
    assert result.output["semantic_coverage"] is False


def test_history_search_uses_only_the_local_git_object_database(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test Engineer"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True
    )
    (tmp_path / "module.py").write_text("needle = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "add searchable needle"], cwd=tmp_path, check=True)
    executable = shutil.which("git")
    assert executable is not None
    version = subprocess.run(
        [executable, "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    policy = policy_for("search.history", Decision.ALLOW, effect_class="read", paths=(".",))
    context = context_for(tmp_path, "search.history", policy, (".",))
    adapter = GitHistorySearchAdapter(Path(executable), version, max_results=20, timeout_seconds=5)

    result = gateway_for(tmp_path, adapter).invoke(
        context,
        {"path": ".", "query": "searchable needle", "field": "message"},
        DeclaredTargets(paths=(".",)),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert [match["subject"] for match in result.output["matches"]] == ["add searchable needle"]
    assert result.output["coverage"] == "committed-history"
    assert result.output["semantic_coverage"] is False
    assert result.output["failures"] == []


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


def test_search_text_uses_bounded_literal_ripgrep_results(tmp_path: Path) -> None:
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "a.txt").write_text("needle one\nneedle two\n", encoding="utf-8")
    (tmp_path / "project" / "b.txt").write_text("needle three\n", encoding="utf-8")
    policy = policy_for("search.text", Decision.ALLOW, effect_class="read", paths=("project",))
    context = context_for(tmp_path, "search.text", policy, ("project",))
    adapter = ripgrep_adapter(max_results=2)

    result = gateway_for(tmp_path, adapter).invoke(
        context,
        {
            "path": "project",
            "query": "needle",
            "semantics": "literal",
            "case": "sensitive",
            "max_results": 2,
        },
        DeclaredTargets(paths=("project",)),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert len(result.output["matches"]) == 2
    assert all(match["submatches"] for match in result.output["matches"])
    assert result.output["engine"] == "ripgrep"
    assert result.output["truncated"] is True
    assert result.output["partial_reason"] == "result_limit"
    assert result.output["continuation_cursor"] is None
    assert "needle" not in str(result.adapter_evidence)


def test_search_text_regex_and_globs_are_passed_without_a_shell(tmp_path: Path) -> None:
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "main.py").write_text("token_42 = True\n", encoding="utf-8")
    (tmp_path / "project" / "main.txt").write_text("token_99\n", encoding="utf-8")
    policy = policy_for("search.text", Decision.ALLOW, effect_class="read", paths=("project",))
    context = context_for(tmp_path, "search.text", policy, ("project",))
    adapter = ripgrep_adapter(max_results=20)

    result = gateway_for(tmp_path, adapter).invoke(
        context,
        {
            "path": "project",
            "query": "token_[0-9]+",
            "semantics": "regex",
            "case": "smart",
            "include": ["*.py"],
        },
        DeclaredTargets(paths=("project",)),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert len(result.output["matches"]) == 1
    assert result.output["matches"][0]["path"].endswith("main.py")
    assert result.adapter_evidence["shell"] is False
