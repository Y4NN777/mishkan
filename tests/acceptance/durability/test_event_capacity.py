from __future__ import annotations

import os
from pathlib import Path
from time import perf_counter

import pytest

from mishkan.application import ApplicationCommand, CommandStatus
from mishkan.persistence import SchemaManager, SQLiteApplicationRepository


@pytest.mark.acceptance
@pytest.mark.performance
def test_event_ingestion_sustains_normative_capacity_for_sixty_seconds(
    tmp_path: Path,
) -> None:
    if os.environ.get("MISHKAN_RUN_PERFORMANCE_GATE") != "1":
        pytest.skip("set MISHKAN_RUN_PERFORMANCE_GATE=1 to run the 60-second capacity gate")

    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    repository = SQLiteApplicationRepository(database)
    required_duration_seconds = 60.0
    required_rate = 100.0
    accepted = 0
    started = perf_counter()

    while perf_counter() - started < required_duration_seconds:
        command = ApplicationCommand(
            command_type="system.checkpoint",
            actor_id="capacity-gate",
            target_type="checkpoint",
            target_id=str(accepted),
            expected_revision=0,
            payload={"sequence": accepted},
        )
        result = repository.accept(
            command,
            target_id=str(accepted),
            event_type="system.checkpoint_recorded",
        )
        assert result.status is CommandStatus.ACCEPTED
        accepted += 1

    elapsed = perf_counter() - started
    observed_rate = accepted / elapsed
    print(
        f"event-capacity-gate accepted={accepted} elapsed={elapsed:.3f}s rate={observed_rate:.2f}/s"
    )

    assert elapsed >= required_duration_seconds
    assert observed_rate >= required_rate
