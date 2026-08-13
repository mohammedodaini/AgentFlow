"""`python -m app.evaluation` end to end (M8).

The only test in the suite that deliberately runs *outside* the rolled-back
transaction, because the thing under test is a process: it opens its own engine,
commits for real, and returns an exit code. Wrapping it in the usual fixture
would test something that does not exist.

That makes cleanup a real assertion rather than a formality. The run creates an
organization, four documents and their chunks in `agentflow_test` and deletes
them again — and if it ever stopped, the next run's recall would quietly improve
because the previous corpus was still there to retrieve from.

**The exit code is what is being tested.** Everything else the CLI prints is for
a human; the exit code is what makes `make eval` a gate rather than a script
somebody runs when they remember to.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.evaluation import __main__ as cli
from app.evaluation.runner import EvalReport, ExampleResult

pytestmark = pytest.mark.usefixtures("database_url")


@pytest.fixture(autouse=True)
def _isolated_artefacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write reports and baselines under `tmp_path`.

    Without this, running the suite would overwrite the committed baseline —
    turning every future regression check into a comparison against whatever the
    last test run happened to produce, which is precisely how a gate becomes a
    formality.
    """
    monkeypatch.setattr(cli, "REPORT_ROOT", tmp_path / "reports")
    monkeypatch.setattr("app.evaluation.runner.BASELINE_ROOT", tmp_path / "baselines")


def test_a_clean_run_exits_zero_and_writes_a_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No baseline exists under `tmp_path`, so there is nothing to regress
    against and the run must pass — the first run of a new dataset should not
    fail for lack of history."""
    monkeypatch.setattr("sys.argv", ["python -m app.evaluation"])

    assert cli.main() == 0

    reports = list((tmp_path / "reports").glob("handbook-*.json"))
    assert len(reports) == 1

    written = json.loads(reports[0].read_text())
    assert written["dataset"] == "handbook"
    assert len(written["examples"]) == 15
    assert written["aggregate"]["recall"] == pytest.approx(1.0)
    assert written["settings"]["chunk_size_tokens"] == 400


def test_a_regression_exits_non_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The assertion the whole milestone rests on.

    A gate that cannot fail is not a gate. This plants a baseline the current
    system cannot meet and requires the process to say so in the only way CI
    reads.
    """
    baselines = tmp_path / "baselines"
    baselines.mkdir(parents=True)
    (baselines / "handbook.json").write_text(
        json.dumps({"dataset": "handbook", "aggregate": {"recall": 1.0, "answer_match": 1.0}}),
        encoding="utf-8",
    )

    monkeypatch.setattr("sys.argv", ["python -m app.evaluation"])

    assert cli.main() == 1, "a fall below the baseline must fail the process"


def test_a_shortfall_inside_the_tolerance_still_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """These numbers are not perfectly stable, so the gate has a tolerance. A
    baseline half a point above the current recall sits inside it."""
    baselines = tmp_path / "baselines"
    baselines.mkdir(parents=True)
    (baselines / "handbook.json").write_text(
        json.dumps({"dataset": "handbook", "aggregate": {"recall": 1.005}}), encoding="utf-8"
    )

    monkeypatch.setattr("sys.argv", ["python -m app.evaluation", "--tolerance", "0.02"])

    assert cli.main() == 0


def test_save_baseline_writes_the_current_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never automatic — an operator asks for it, having read the report.
    Overwriting on every run would make each later comparison pass by
    construction."""
    monkeypatch.setattr("sys.argv", ["python -m app.evaluation", "--save-baseline"])

    assert cli.main() == 0

    stored = json.loads((tmp_path / "baselines" / "handbook.json").read_text())
    assert stored["aggregate"]["recall"] == pytest.approx(1.0)
    assert stored["settings"]["embedding_provider"] == "hashing"


def test_the_run_leaves_no_corpus_behind(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted against the real database from outside the process that wrote
    it — the only way to be sure the commit left nothing behind.

    Synchronous, like every test in this file, because `cli.main()` calls
    `asyncio.run` and that cannot nest inside a running loop. The verification
    query gets its own `asyncio.run` afterwards, once the CLI's loop has closed.
    """
    monkeypatch.setattr("sys.argv", ["python -m app.evaluation"])
    cli.main()

    async def count_leftovers() -> int:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                remaining: int = await connection.scalar(
                    text("SELECT count(*) FROM organizations WHERE name LIKE 'eval-%'")
                )
                return remaining
        finally:
            await engine.dispose()

    assert asyncio.run(count_leftovers()) == 0


def test_the_printed_summary_lists_only_examples_needing_attention(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Failures only, not every row. A report listing everything is one nobody
    reads to the end, and the rows that matter are the ones somebody must act
    on."""
    report = EvalReport(dataset="d", generated_at="now", settings={})
    report.results = [
        ExampleResult(
            example_id="fine", question="?", answer="a", relevant_document_ids=["x"], recall=1.0
        ),
        ExampleResult(
            example_id="missed", question="?", answer="a", relevant_document_ids=["x"], recall=0.0
        ),
        ExampleResult(
            example_id="answered-anyway", question="?", answer="a", refusal_correct=False
        ),
    ]

    cli._print(report, [], tmp_path / "r.json", 0.02)  # noqa: SLF001

    printed = capsys.readouterr().out
    assert "missed" in printed
    assert "answered-anyway" in printed
    assert "should have refused" in printed
    assert "fine" not in printed
    assert "no regression against baseline" in printed
