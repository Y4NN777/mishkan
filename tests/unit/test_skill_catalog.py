from pathlib import Path

import pytest

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.skills import (
    SkillActivationState,
    SkillBounds,
    SkillCatalog,
    SkillSelectionContext,
    SkillSourceDefinition,
    SkillSourceKind,
    SkillTrustState,
    SkillUseOutcome,
)


def _bounds(*, extensions: bool = False) -> SkillBounds:
    return SkillBounds(
        max_frontmatter_bytes=8_192,
        max_manifest_bytes=64_000,
        max_resource_bytes=64_000,
        max_package_bytes=256_000,
        max_package_files=100,
        max_resource_depth=2,
        allow_frontmatter_extensions=extensions,
    )


def _source(
    uri: str,
    *,
    source_id: str = "project-skills",
    precedence: int = 100,
    activation: SkillActivationState = SkillActivationState.ACTIVE,
) -> SkillSourceDefinition:
    return SkillSourceDefinition(
        source_id=source_id,
        kind=SkillSourceKind.PROJECT,
        uri=uri,
        revision="git:abc123",
        precedence=precedence,
        trust=SkillTrustState.TRUSTED,
        default_activation=activation,
    )


def _write_skill(
    root: Path,
    name: str = "code-review",
    *,
    description: str = "Review code changes. Use for software review tasks.",
    required_tools: tuple[str, ...] = ("file.read",),
    fallback_tools: dict[str, tuple[str, ...]] | None = None,
    task_classes: tuple[str, ...] = ("software.review",),
    reference: bool = True,
    extra_frontmatter: str = "",
) -> Path:
    package = root / name
    package.mkdir(parents=True)
    fallback_lines = ""
    if fallback_tools:
        fallback_lines = "    fallback_tools:\n" + "".join(
            f"      {tool}: [{', '.join(alternatives)}]\n"
            for tool, alternatives in fallback_tools.items()
        )
    reference_line = (
        "Read [the checklist](references/checklist.md) when needed.\n" if reference else ""
    )
    manifest = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "license: Apache-2.0\n"
        "allowed-tools: file.read\n"
        f"{extra_frontmatter}"
        "metadata:\n"
        "  version: 1.2.3\n"
        "  author: test-author\n"
        "  mishkan:\n"
        "    platforms: [linux, darwin]\n"
        f"    required_tools: [{', '.join(required_tools)}]\n"
        f"{fallback_lines}"
        "    organization_versions: ['organization:59@1']\n"
        f"    task_classes: [{', '.join(task_classes)}]\n"
        "---\n"
        f"# {name}\n\n"
        "Follow the task contract and do not infer tool authority.\n"
        f"{reference_line}"
    )
    (package / "SKILL.md").write_text(manifest, encoding="utf-8")
    references = package / "references"
    references.mkdir()
    (references / "checklist.md").write_text("# Review checklist\n", encoding="utf-8")
    return package


def _context(*, tools: frozenset[str] = frozenset({"file.read"})) -> SkillSelectionContext:
    return SkillSelectionContext(
        task_id="task-1",
        task_class="software.review",
        consuming_identity="quality-engineer",
        platform="linux",
        organization_version="organization:59@1",
        available_tools=tools,
    )


def test_level_zero_discovery_selection_and_progressive_loading(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills)
    catalog = SkillCatalog((_source("project:skills"),), tmp_path, bounds=_bounds())

    metadata = catalog.list_metadata()
    assert len(metadata) == 1
    assert metadata[0].name == "code-review"
    assert metadata[0].resource_paths == ("references/checklist.md",)
    assert catalog.search("software") == metadata

    selection = catalog.select("code-review", _context())
    assert selection.outcome is SkillUseOutcome.HIT
    loaded = catalog.load_instructions(selection)
    assert loaded.evidence.instruction_fingerprint is not None
    assert loaded.evidence.loaded_resources == ()

    loaded = catalog.load_resource(loaded, "references/checklist.md")
    assert loaded.evidence.loaded_resources[0].path == "references/checklist.md"
    assert loaded.resources[0][1] == b"# Review checklist\n"
    assert catalog.load_resource(loaded, "references/checklist.md") == loaded


def test_selection_reports_partial_fallback_and_miss(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(
        skills,
        required_tools=("search.structural",),
        fallback_tools={"search.structural": ("search.text",)},
    )
    catalog = SkillCatalog((_source("project:skills"),), tmp_path, bounds=_bounds())

    partial = catalog.select("code-review", _context(tools=frozenset({"search.text"})))
    assert partial.outcome is SkillUseOutcome.PARTIAL
    assert partial.fallback_bindings == {"search.structural": "search.text"}

    missing = catalog.select("code-review", _context(tools=frozenset()))
    assert missing.outcome is SkillUseOutcome.MISS
    assert missing.missing_conditions == ("tool:search.structural",)

    unknown = catalog.select("not-installed", _context())
    assert unknown.outcome is SkillUseOutcome.MISS
    assert unknown.selected is None
    with pytest.raises(MishkanError, match="cannot be loaded for a miss"):
        catalog.load_instructions(unknown)


def test_inactive_or_incompatible_skill_cannot_load(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills)
    catalog = SkillCatalog(
        (_source("project:skills", activation=SkillActivationState.CANDIDATE),),
        tmp_path,
        bounds=_bounds(),
    )
    selection = catalog.select("code-review", _context())
    assert selection.outcome is SkillUseOutcome.MISS
    assert selection.missing_conditions == ("activation:candidate",)


def test_resource_must_be_in_package_and_referenced_by_instructions(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, reference=False)
    catalog = SkillCatalog((_source("project:skills"),), tmp_path, bounds=_bounds())
    loaded = catalog.load_instructions(catalog.select("code-review", _context()))

    with pytest.raises(MishkanError, match="was not referenced"):
        catalog.load_resource(loaded, "references/checklist.md")
    with pytest.raises(MishkanError, match="path is invalid"):
        catalog.load_resource(loaded, "../secret")


def test_package_drift_blocks_level_one_load(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    package = _write_skill(skills)
    catalog = SkillCatalog((_source("project:skills"),), tmp_path, bounds=_bounds())
    selection = catalog.select("code-review", _context())
    (package / "references/checklist.md").write_text("changed", encoding="utf-8")

    with pytest.raises(MishkanError, match="changed after catalogue selection"):
        catalog.load_instructions(selection)


def test_precedence_is_deterministic_and_equal_precedence_refuses(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_skill(first, description="Lower precedence description.")
    _write_skill(second, description="Higher precedence description.")
    selected = SkillCatalog(
        (
            _source(str(first), source_id="first", precedence=10),
            _source(str(second), source_id="second", precedence=20),
        ),
        tmp_path,
        bounds=_bounds(),
    )
    assert selected.list_metadata()[0].source_id == "second"

    with pytest.raises(MishkanError) as caught:
        SkillCatalog(
            (
                _source(str(first), source_id="first", precedence=20),
                _source(str(second), source_id="second", precedence=20),
            ),
            tmp_path,
            bounds=_bounds(),
        )
    assert caught.value.envelope.code is ErrorCode.SKILL_SELECTION


def test_frontmatter_extensions_are_publicly_configurable(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, extra_frontmatter="owner: quality-team\n")
    with pytest.raises(MishkanError, match="unsupported frontmatter"):
        SkillCatalog((_source("project:skills"),), tmp_path, bounds=_bounds())

    catalog = SkillCatalog(
        (_source("project:skills"),),
        tmp_path,
        bounds=_bounds(extensions=True),
    )
    assert catalog.list_metadata()[0].name == "code-review"


def test_remote_source_requires_local_acquisition_lock(tmp_path: Path) -> None:
    source = SkillSourceDefinition(
        source_id="community-hub",
        kind=SkillSourceKind.URL,
        uri="https://example.invalid/skills",
        revision="sha256:known",
        precedence=10,
        trust=SkillTrustState.UNTRUSTED,
        default_activation=SkillActivationState.CANDIDATE,
    )
    with pytest.raises(MishkanError, match="acquired local provenance lock"):
        SkillCatalog((source,), tmp_path, bounds=_bounds())
