from __future__ import annotations

import inspect
from pathlib import Path

from crewai import Agent, Crew, Process, Task
from crewai.flow.flow import Flow
from pydantic import ValidationError

from mishkan.config.loader import ConfigLoader
from mishkan.crewai.coordinator import CrewAIInitializationCoordinator
from mishkan.crewai.flow import CrewAIInitializationFlow
from mishkan.crewai.routing import CrewAIModelRouter
from mishkan.planning.models import (
    InitializationResult,
    PlanCandidate,
    PlannedToolCall,
    PlanTask,
    ReviewDecision,
)


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
    assert llm.timeout == config.value.crewai.model_timeout_seconds
    assert llm.max_tokens == config.value.crewai.model_max_output_tokens
    assert llm.max_retries == config.value.crewai.model_transport_retries


def test_structured_failure_feedback_exposes_no_rejected_values() -> None:
    canary = "secret-rejected-tool-value"
    try:
        PlanTask(
            task_id="inspect-repository",
            title="Inspect repository",
            purpose="Inspect bounded repository evidence.",
            assigned_role="Repository_Investigator",
            tools=("repository.read_file",),
            tool_calls=(
                PlannedToolCall(
                    call_id="invalid-call",
                    tool_id=canary,
                    arguments={},
                ),
            ),
            evidence_paths=("README.md",),
        )
    except ValidationError as exc:
        locations = CrewAIInitializationCoordinator._validation_failure_locations(exc)
    else:
        raise AssertionError("invalid planned tool selection was accepted")

    assert locations == ("<root> [value_error]",)
    assert canary not in str(locations)


def test_provider_visible_structured_schemas_use_portable_string_bounds() -> None:
    def maximum_string_bound(value: object) -> int:
        if isinstance(value, dict):
            own = value.get("maxLength")
            nested = [maximum_string_bound(item) for item in value.values()]
            return max([own if isinstance(own, int) else 0, *nested])
        if isinstance(value, list):
            return max((maximum_string_bound(item) for item in value), default=0)
        return 0

    for output_model in (PlanCandidate, InitializationResult, ReviewDecision):
        assert maximum_string_bound(output_model.model_json_schema()) <= 2_000
