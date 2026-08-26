from __future__ import annotations

import hashlib
from pathlib import Path

from support.i02 import context_for, inspector, policy_for

from mishkan.policy import Decision, PolicyAuthority
from mishkan.tools.adapters import (
    CapabilityAdapter,
    FileListAdapter,
    FileReadAdapter,
    FileResolveAdapter,
    FileStatAdapter,
)
from mishkan.tools.gateway import CapabilityGateway, MappingCredentialResolver, MemoryEvidenceSink
from mishkan.tools.gateway_models import CallStatus, DeclaredTargets


def gateway_for(root: Path, adapter: CapabilityAdapter, adapter_id: str) -> CapabilityGateway:
    return CapabilityGateway(
        root,
        PolicyAuthority(),
        MappingCredentialResolver({}),
        inspector(root),
        {adapter_id: adapter},
        MemoryEvidenceSink(),
    )


def test_file_resolve_returns_attributable_object_evidence(tmp_path: Path) -> None:
    target = tmp_path / "src" / "main.py"
    target.parent.mkdir()
    target.write_text("print('ok')\n", encoding="utf-8")
    policy = policy_for("file.resolve", Decision.ALLOW, effect_class="read", paths=("src/*",))
    context = context_for(tmp_path, "file.resolve", policy, ("src/*",))
    adapter = FileResolveAdapter()

    result = gateway_for(tmp_path, adapter, adapter.adapter_id).invoke(
        context,
        {"path": "src/main.py"},
        DeclaredTargets(paths=("src/main.py",)),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output == {
        "requested_path": "src/main.py",
        "lexical_path": "src/main.py",
        "resolved_path": "src/main.py",
        "exists": True,
        "object_type": "file",
        "is_symlink": False,
        "link_chain": [],
    }


def test_file_resolve_reports_an_in_scope_symlink_chain(tmp_path: Path) -> None:
    (tmp_path / "actual.txt").write_text("actual", encoding="utf-8")
    (tmp_path / "alias.txt").symlink_to("actual.txt")
    policy = policy_for(
        "file.resolve",
        Decision.ALLOW,
        effect_class="read",
        paths=("actual.txt",),
    )
    context = context_for(tmp_path, "file.resolve", policy, ("actual.txt",))
    adapter = FileResolveAdapter()

    result = gateway_for(tmp_path, adapter, adapter.adapter_id).invoke(
        context,
        {"path": "alias.txt"},
        DeclaredTargets(paths=("alias.txt",)),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert result.output["lexical_path"] == "alias.txt"
    assert result.output["resolved_path"] == "actual.txt"
    assert result.output["is_symlink"] is True
    assert result.output["link_chain"] == ["alias.txt->actual.txt"]


def test_file_resolve_refuses_a_symlink_cycle_before_dispatch(tmp_path: Path) -> None:
    (tmp_path / "a").symlink_to("b")
    (tmp_path / "b").symlink_to("a")
    policy = policy_for("file.resolve", Decision.ALLOW, effect_class="read", paths=("*",))
    context = context_for(tmp_path, "file.resolve", policy, ("*",))
    adapter = FileResolveAdapter()

    result = gateway_for(tmp_path, adapter, adapter.adapter_id).invoke(
        context,
        {"path": "a"},
        DeclaredTargets(paths=("a",)),
    )

    assert result.status is CallStatus.REFUSED
    assert result.error_code == "ERR-FIL-001"


def test_file_stat_returns_optional_digest_and_platform_identity(tmp_path: Path) -> None:
    target = tmp_path / "README.md"
    target.write_bytes(b"evidence\n")
    policy = policy_for("file.stat", Decision.ALLOW, effect_class="read", paths=("README.md",))
    context = context_for(tmp_path, "file.stat", policy, ("README.md",))
    adapter = FileStatAdapter(max_digest_bytes=1024)

    result = gateway_for(tmp_path, adapter, adapter.adapter_id).invoke(
        context,
        {"path": "README.md", "digest": True},
        DeclaredTargets(paths=("README.md",)),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert result.output["path"] == "README.md"
    assert result.output["object_type"] == "file"
    assert result.output["size"] == 9
    expected_digest = hashlib.sha256(b"evidence\n").hexdigest()
    assert result.output["digest"] == f"sha256:{expected_digest}"
    assert result.output["object_identity"]["device"] >= 0
    assert result.output["object_identity"]["inode"] >= 0


def test_file_read_is_bounded_and_returns_continuation_evidence(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    policy = policy_for("file.read", Decision.ALLOW, effect_class="read", paths=("notes.txt",))
    context = context_for(tmp_path, "file.read", policy, ("notes.txt",))
    adapter = FileReadAdapter(max_bytes=8, max_scan_bytes=1024)

    result = gateway_for(tmp_path, adapter, adapter.adapter_id).invoke(
        context,
        {
            "path": "notes.txt",
            "mode": "text",
            "offset": 0,
            "max_bytes": 5,
            "encoding": "utf-8",
            "binary_policy": "reject",
        },
        DeclaredTargets(paths=("notes.txt",)),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert result.output["content"] == "alpha"
    assert result.output["content_format"] == "text"
    assert result.output["byte_range"] == [0, 5]
    assert result.output["total_bytes"] == 17
    assert result.output["truncated"] is True
    assert result.output["continuation_offset"] == 5
    assert result.output["changed_during_read"] is False


def test_file_read_lines_preserves_requested_line_range(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    policy = policy_for("file.read", Decision.ALLOW, effect_class="read", paths=("notes.txt",))
    context = context_for(tmp_path, "file.read", policy, ("notes.txt",))
    adapter = FileReadAdapter(max_bytes=64, max_scan_bytes=1024)

    result = gateway_for(tmp_path, adapter, adapter.adapter_id).invoke(
        context,
        {
            "path": "notes.txt",
            "mode": "lines",
            "start_line": 2,
            "end_line": 2,
            "max_bytes": 64,
            "encoding": "utf-8",
            "binary_policy": "reject",
        },
        DeclaredTargets(paths=("notes.txt",)),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert result.output["content"] == "beta\n"
    assert result.output["line_range"] == [2, 2]
    assert result.output["truncated"] is False


def test_file_read_binary_policy_is_explicit(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"a\x00b")
    policy = policy_for("file.read", Decision.ALLOW, effect_class="read", paths=("blob.bin",))
    context = context_for(tmp_path, "file.read", policy, ("blob.bin",))
    adapter = FileReadAdapter(max_bytes=64, max_scan_bytes=1024)
    gateway = gateway_for(tmp_path, adapter, adapter.adapter_id)

    refused = gateway.invoke(
        context,
        {"path": "blob.bin", "mode": "text", "binary_policy": "reject"},
        DeclaredTargets(paths=("blob.bin",)),
    )
    encoded = gateway.invoke(
        context,
        {"path": "blob.bin", "mode": "text", "binary_policy": "base64"},
        DeclaredTargets(paths=("blob.bin",)),
    )

    assert refused.status is CallStatus.FAILED
    assert refused.error_code == "ERR-FIL-001"
    assert encoded.status is CallStatus.COMPLETED
    assert encoded.output is not None
    assert encoded.output["content"] == "YQBi"
    assert encoded.output["content_format"] == "base64"
    assert encoded.output["encoding"] is None


def test_file_read_truncation_does_not_emit_partial_unicode(tmp_path: Path) -> None:
    (tmp_path / "unicode.txt").write_text("aéx", encoding="utf-8")
    policy = policy_for("file.read", Decision.ALLOW, effect_class="read", paths=("unicode.txt",))
    context = context_for(tmp_path, "file.read", policy, ("unicode.txt",))
    adapter = FileReadAdapter(max_bytes=2, max_scan_bytes=1024)

    result = gateway_for(tmp_path, adapter, adapter.adapter_id).invoke(
        context,
        {"path": "unicode.txt", "mode": "text", "max_bytes": 2, "encoding": "utf-8"},
        DeclaredTargets(paths=("unicode.txt",)),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert result.output["content"] == "a"
    assert result.output["byte_range"] == [0, 1]
    assert result.output["continuation_offset"] == 1


def test_file_read_refuses_bounds_that_cannot_advance(tmp_path: Path) -> None:
    (tmp_path / "unicode.txt").write_text("é", encoding="utf-8")
    policy = policy_for("file.read", Decision.ALLOW, effect_class="read", paths=("unicode.txt",))
    context = context_for(tmp_path, "file.read", policy, ("unicode.txt",))
    adapter = FileReadAdapter(max_bytes=1, max_scan_bytes=1024)

    result = gateway_for(tmp_path, adapter, adapter.adapter_id).invoke(
        context,
        {"path": "unicode.txt", "mode": "text", "max_bytes": 1, "encoding": "utf-8"},
        DeclaredTargets(paths=("unicode.txt",)),
    )

    assert result.status is CallStatus.FAILED
    assert result.error_code == "ERR-FIL-001"


def test_file_read_rejects_offsets_and_lines_beyond_end(tmp_path: Path) -> None:
    (tmp_path / "short.txt").write_text("one\n", encoding="utf-8")
    policy = policy_for("file.read", Decision.ALLOW, effect_class="read", paths=("short.txt",))
    context = context_for(tmp_path, "file.read", policy, ("short.txt",))
    adapter = FileReadAdapter(max_bytes=64, max_scan_bytes=1024)
    gateway = gateway_for(tmp_path, adapter, adapter.adapter_id)

    offset_result = gateway.invoke(
        context,
        {"path": "short.txt", "mode": "text", "offset": 5},
        DeclaredTargets(paths=("short.txt",)),
    )
    line_result = gateway.invoke(
        context,
        {"path": "short.txt", "mode": "lines", "start_line": 2},
        DeclaredTargets(paths=("short.txt",)),
    )

    assert offset_result.status is CallStatus.FAILED
    assert offset_result.error_code == "ERR-FIL-001"
    assert line_result.status is CallStatus.FAILED
    assert line_result.error_code == "ERR-FIL-001"


def test_file_read_supports_empty_line_views(tmp_path: Path) -> None:
    (tmp_path / "empty.txt").touch()
    policy = policy_for("file.read", Decision.ALLOW, effect_class="read", paths=("empty.txt",))
    context = context_for(tmp_path, "file.read", policy, ("empty.txt",))
    adapter = FileReadAdapter(max_bytes=64, max_scan_bytes=1024)

    result = gateway_for(tmp_path, adapter, adapter.adapter_id).invoke(
        context,
        {"path": "empty.txt", "mode": "head", "line_count": 10},
        DeclaredTargets(paths=("empty.txt",)),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert result.output["content"] == ""
    assert result.output["line_range"] is None


def test_file_list_is_recursive_filtered_and_deterministic(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "z.py").write_text("z", encoding="utf-8")
    (tmp_path / "src" / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "src" / "notes.txt").write_text("notes", encoding="utf-8")
    (tmp_path / "src" / ".secret.py").write_text("secret", encoding="utf-8")
    policy = policy_for("file.list", Decision.ALLOW, effect_class="read", paths=("src",))
    context = context_for(tmp_path, "file.list", policy, ("src",))
    adapter = FileListAdapter(max_results=10, max_traversal_entries=100)

    result = gateway_for(tmp_path, adapter, adapter.adapter_id).invoke(
        context,
        {
            "path": "src",
            "recursive": True,
            "max_depth": 2,
            "include": ["*.py"],
            "exclude": ["*z.py"],
            "include_hidden": False,
        },
        DeclaredTargets(paths=("src",)),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert [entry["path"] for entry in result.output["entries"]] == ["a.py"]
    assert result.output["ordering"] == "path-bytewise-ascending"
    assert result.output["ignore_evidence"]["hidden"] == "excluded"
    assert result.output["truncated"] is False


def test_file_list_cursor_is_bound_to_the_query(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / "src" / name).touch()
    policy = policy_for("file.list", Decision.ALLOW, effect_class="read", paths=("src",))
    context = context_for(tmp_path, "file.list", policy, ("src",))
    adapter = FileListAdapter(max_results=2, max_traversal_entries=100)
    gateway = gateway_for(tmp_path, adapter, adapter.adapter_id)
    arguments = {"path": "src", "include": ["*.py"], "max_results": 2}

    first = gateway.invoke(context, arguments, DeclaredTargets(paths=("src",)))

    assert first.status is CallStatus.COMPLETED
    assert first.output is not None
    assert [entry["path"] for entry in first.output["entries"]] == ["a.py", "b.py"]
    assert first.output["truncated"] is True
    cursor = first.output["continuation_cursor"]
    second = gateway.invoke(
        context,
        {**arguments, "cursor": cursor},
        DeclaredTargets(paths=("src",)),
    )
    mismatched = gateway.invoke(
        context,
        {**arguments, "include_hidden": True, "cursor": cursor},
        DeclaredTargets(paths=("src",)),
    )

    assert second.status is CallStatus.COMPLETED
    assert second.output is not None
    assert [entry["path"] for entry in second.output["entries"]] == ["c.py"]
    assert second.output["continuation_cursor"] is None
    assert mismatched.status is CallStatus.FAILED
    assert mismatched.error_code == "ERR-FIL-001"

    (tmp_path / "src" / "aa.py").touch()
    changed = gateway.invoke(
        context,
        {**arguments, "cursor": cursor},
        DeclaredTargets(paths=("src",)),
    )
    assert changed.status is CallStatus.FAILED
    assert changed.error_code == "ERR-FIL-001"


def test_file_list_does_not_follow_links_implicitly(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "inside.txt").touch()
    (tmp_path / "src" / "linked").symlink_to(tmp_path / "target", target_is_directory=True)
    policy = policy_for("file.list", Decision.ALLOW, effect_class="read", paths=("src",))
    context = context_for(tmp_path, "file.list", policy, ("src",))
    adapter = FileListAdapter(max_results=10, max_traversal_entries=100)

    result = gateway_for(tmp_path, adapter, adapter.adapter_id).invoke(
        context,
        {"path": "src", "recursive": True},
        DeclaredTargets(paths=("src",)),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert [entry["path"] for entry in result.output["entries"]] == ["linked"]
    assert result.output["entries"][0]["object_type"] == "symlink"
