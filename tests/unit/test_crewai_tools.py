from pathlib import Path

import pytest

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.tools.crewai_tools import ReadRepositoryFileTool
from mishkan.tools.registry import load_tool_registry


def _tool(root: Path, *allowed: str) -> ReadRepositoryFileTool:
    return ReadRepositoryFileTool(
        load_tool_registry().require("repository.read_file"),
        root,
        allowed,
    )


def test_read_tool_reads_only_an_exact_bound_path(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("bound evidence", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("not bound", encoding="utf-8")

    assert _tool(tmp_path, "README.md").run(path="README.md") == "bound evidence"
    with pytest.raises(MishkanError) as caught:
        _tool(tmp_path, "README.md").run(path="secret.txt")
    assert caught.value.envelope.code is ErrorCode.AUTHORITY_NOT_GRANTED


def test_read_tool_refuses_a_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)

    with pytest.raises(MishkanError) as caught:
        _tool(tmp_path, "link.txt").run(path="link.txt")
    assert caught.value.envelope.code is ErrorCode.AUTHORITY_NOT_GRANTED


def test_read_limit_is_registry_configurable(tmp_path: Path) -> None:
    registry_path = tmp_path / "tools.yaml"
    registry_path.write_text(
        """schema_version: '1.0'
tools:
  - tool_id: repository.read_file
    crewai_name: repository_read_file
    description: Read one bound file.
    effect: read
    max_bytes: 3
""",
        encoding="utf-8",
    )
    (tmp_path / "evidence.txt").write_text("four", encoding="utf-8")
    tool = ReadRepositoryFileTool(
        load_tool_registry(registry_path).require("repository.read_file"),
        tmp_path,
        ("evidence.txt",),
    )

    with pytest.raises(MishkanError) as caught:
        tool.run(path="evidence.txt")
    assert caught.value.envelope.code is ErrorCode.TOOL_EFFECT
