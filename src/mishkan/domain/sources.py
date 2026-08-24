"""Resolution rules for explicitly project-scoped configuration sources."""

from pathlib import Path

from mishkan.domain.errors import ErrorCode, MishkanError


def resolve_source_path(uri: str, project_root: Path, source_kind: str) -> Path:
    """Resolve a file URI while preventing `project:` traversal and symlink escape."""
    root = project_root.resolve()
    project_scoped = uri.startswith("project:")
    raw = uri.removeprefix("project:") if project_scoped else uri
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if project_scoped and not resolved.is_relative_to(root):
        raise MishkanError(
            ErrorCode.AUTHORITY_NOT_GRANTED,
            f"project-scoped {source_kind} resolves outside the project",
            details={"source": uri},
        )
    return resolved
