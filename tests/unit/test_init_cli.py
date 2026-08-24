import json
from pathlib import Path

from typer.testing import CliRunner

from mishkan.application.initialize import MishkanInitializer
from mishkan.cli.app import app
from mishkan.planning.models import InitializationReport, InitializationResult, ReviewDecision


def test_init_cli_emits_machine_readable_reviewed_report(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    result = InitializationResult(
        repository_revision="a" * 40,
        task_id="inspect-readme",
        summary="Evidence-backed result.",
        cited_paths=("README.md",),
        findings=("The repository has a README.",),
    )
    review = ReviewDecision(
        task_id=result.task_id,
        verdict="accepted",
        summary="Independent review passed.",
        checked_citations=result.cited_paths,
    )
    report = InitializationReport(
        run_id="run-id",
        repository_id="b" * 64,
        repository_revision=result.repository_revision,
        discovery_fingerprint="c" * 64,
        plan_fingerprint="d" * 64,
        resumed=False,
        completed_task_ids=(result.task_id,),
        results=(result,),
        reviews=(review,),
    )
    monkeypatch.setattr(MishkanInitializer, "run", lambda *_args, **_kwargs: report)

    cli_result = CliRunner().invoke(
        app,
        [
            "--json",
            "--config",
            str(Path("tests/fixtures/config/local-valid.yaml")),
            "init",
            "Inspect repository evidence",
            "--repository",
            ".",
        ],
    )

    assert cli_result.exit_code == 0, cli_result.output
    payload = json.loads(cli_result.stdout)
    assert payload["results"][0]["task_id"] == "inspect-readme"
    assert payload["reviews"][0]["verdict"] == "accepted"
