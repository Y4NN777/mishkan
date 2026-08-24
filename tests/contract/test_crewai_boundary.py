from __future__ import annotations

import inspect
from pathlib import Path

from crewai import Agent, Crew, Process, Task
from crewai.flow.flow import Flow

from mishkan.config.loader import ConfigLoader
from mishkan.crewai.coordinator import CrewAIInitializationCoordinator
from mishkan.crewai.flow import CrewAIInitializationFlow
from mishkan.crewai.routing import CrewAIModelRouter


def test_production_boundary_is_real_crewai_without_a_runtime_selector() -> None:
    parameters = inspect.signature(CrewAIInitializationCoordinator).parameters

    assert "runtime" not in parameters
    assert "runtime_selector" not in parameters
    assert issubclass(CrewAIInitializationFlow, Flow)
    assert CrewAIInitializationFlow._skip_auto_memory is True
    assert Agent.__module__.startswith("crewai.")
    assert Task.__module__.startswith("crewai.")
    assert Crew.__module__.startswith("crewai.")
    assert Process.sequential.value == "sequential"


def test_local_route_materializes_a_crewai_ollama_llm() -> None:
    config = ConfigLoader().load([Path("tests/fixtures/config/local-valid.yaml")])
    llm = next(CrewAIModelRouter(config.value).candidates_for("planning"))

    assert llm.__class__.__module__.startswith("crewai.")
    assert llm.model == "fixture-model"
    assert llm.base_url == "http://127.0.0.1:11434/v1"
