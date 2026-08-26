"""Configure CrewAI before importing its runtime modules."""

import os
from pathlib import Path

from mishkan.config.models import CrewAIRuntimeConfig


def configure_crewai_environment(
    runtime: CrewAIRuntimeConfig,
    storage_root: Path | None = None,
) -> None:
    """Apply explicit privacy settings before CrewAI initializes listeners."""

    if not runtime.telemetry:
        os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
        os.environ["OTEL_SDK_DISABLED"] = "true"
    if storage_root is not None:
        resolved = storage_root.resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        os.environ["XDG_DATA_HOME"] = str(resolved)
        os.environ["CREWAI_STORAGE_DIR"] = "mishkan-crewai"
