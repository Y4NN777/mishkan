"""Public, versioned I01 tool registry."""

from importlib.resources import files
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str = Field(min_length=1)
    crewai_name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    description: str = Field(min_length=3)
    effect: str = Field(pattern="^read$")
    max_bytes: int = Field(ge=1, le=10_000_000)


class ToolRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    tools: tuple[ToolDefinition, ...] = Field(min_length=1)

    def require(self, tool_id: str) -> ToolDefinition:
        matches = [tool for tool in self.tools if tool.tool_id == tool_id]
        if len(matches) != 1:
            raise ValueError(f"tool registry does not contain exactly one {tool_id!r}")
        return matches[0]


def load_tool_registry(source: Path | None = None) -> ToolRegistry:
    if source is None:
        resource = files("mishkan.resources.tools").joinpath("i01-tools.yaml")
        document = yaml.safe_load(resource.read_text(encoding="utf-8"))
    else:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    return ToolRegistry.model_validate(document)
