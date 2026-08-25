"""Typed tool contracts and exact task bindings."""

from mishkan.tools.catalog import ToolCatalog
from mishkan.tools.crewai_gateway import GatewayCrewAITool
from mishkan.tools.execution import (
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    ShellDialect,
    ShellOptions,
    ShellProfile,
)
from mishkan.tools.models import RegistrySnapshot, ToolBinding, ToolContract
from mishkan.tools.native import (
    NativeCapabilityEnvironment,
    available_contracts,
    build_native_adapters,
    discover_native_environment,
)

__all__ = [
    "ExecutionMode",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionStatus",
    "GatewayCrewAITool",
    "NativeCapabilityEnvironment",
    "RegistrySnapshot",
    "ShellDialect",
    "ShellOptions",
    "ShellProfile",
    "ToolBinding",
    "ToolCatalog",
    "ToolContract",
    "available_contracts",
    "build_native_adapters",
    "discover_native_environment",
]
