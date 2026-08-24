"""Configure CrewAI before importing its runtime modules."""

import os

from mishkan.config.models import CrewAIRuntimeConfig


def configure_crewai_environment(runtime: CrewAIRuntimeConfig) -> None:
    """Apply explicit privacy settings before CrewAI initializes listeners."""

    if not runtime.telemetry:
        os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
        os.environ["OTEL_SDK_DISABLED"] = "true"
