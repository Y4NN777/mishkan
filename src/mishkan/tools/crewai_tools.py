"""CrewAI-native representations of exact, read-only task tool bindings."""

from pathlib import Path

from crewai.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.tools.registry import ToolDefinition


class ReadRepositoryFileInput(BaseModel):
    path: str = Field(description="Exact repository-relative path bound to this task.")


class ReadRepositoryFileTool(BaseTool):
    _root: Path = PrivateAttr()
    _allowed_paths: frozenset[str] = PrivateAttr()
    _max_bytes: int = PrivateAttr()

    def __init__(
        self,
        definition: ToolDefinition,
        repository_root: Path,
        allowed_paths: tuple[str, ...],
    ) -> None:
        super().__init__(
            name=definition.crewai_name,
            description=definition.description,
            args_schema=ReadRepositoryFileInput,
        )
        self._root = repository_root.resolve()
        self._allowed_paths = frozenset(Path(path).as_posix() for path in allowed_paths)
        self._max_bytes = definition.max_bytes

    def _run(self, path: str) -> str:
        relative = Path(path)
        normalized = relative.as_posix()
        if relative.is_absolute() or normalized not in self._allowed_paths:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "repository path is not bound to this task",
                details={"path": normalized},
            )
        resolved = (self._root / relative).resolve()
        if not resolved.is_relative_to(self._root):
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "repository path resolves outside the bound root",
                details={"path": normalized},
            )
        try:
            content = resolved.read_bytes()
        except OSError as exc:
            raise MishkanError(
                ErrorCode.TOOL_UNAVAILABLE,
                "bound repository file cannot be read",
                details={"path": normalized, "reason": type(exc).__name__},
            ) from exc
        if len(content) > self._max_bytes:
            raise MishkanError(
                ErrorCode.TOOL_EFFECT,
                "bound repository file exceeds the configured read limit",
                details={"path": normalized, "max_bytes": self._max_bytes},
            )
        return content.decode("utf-8", errors="replace")
