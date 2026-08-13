"""The eval runner, against a real database (M8).

Integration, because a run that did not really ingest, really embed and really
query pgvector would measure fixtures rather than the pipeline — and the whole
purpose of M8 is that these numbers move when chunk geometry moves.

The tests that matter most are the ones about the *gate*. A regression detector
that never fires is worse than none: it turns "no regression" from a claim into
a formality, and everyone stops reading it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.evaluation.judge import HeuristicJudge
from app.evaluation.runner import (
    EvalReport,
    ExampleResult,
    compare_to_baseline,
    load_baseline,
    run_eval,
    save_baseline,
)
from app.llm.offline import OfflineLLM
from app.models.organization import Organization
from app.rag.embeddings import EmbeddingProvider
from app.rag.generation import Generator


async def run(
    session: AsyncSession, embedder: EmbeddingProvider, **overrides: object
) -> EvalReport:
    settings = get_settings().model_copy(update=overrides)
    return await run_eval(
        session,
        embedder,
        Generator(session, embedder, OfflineLLM(), settings),
        HeuristicJudge(),
        settings,
        dataset_name="handbook",
    )


# --------------------------------------------------------------------------
# a run end to end
# --------------------------------------------------------------------------


async def test_a_run_scores_every_example(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    report = await run(db_session, embedder)

    assert len(report.results) == 15
    assert report.aggregate["recall"] > 0.0


async def test_retrieval_finds_the_right_document_for_answerable_questions(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The headline retrieval result, asserted rather than merely printed.

    If this drops, nothing downstream can recover: a passage that was never
    retrieved cannot be cited, quoted or reasoned over, however good the model.
    """
    report = await run(db_session, embedder)

    assert report.aggregate["recall"] == pytest.approx(1.0), [
        (result.example_id, result.retrieved_document_ids)
        for result in report.results
        if result.recall < 1.0
    ]


async def test_the_report_records_the_settings_that_produced_it(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Without them a report is a number with no experiment attached — and the
    point of M8 is comparing runs that differ by exactly one setting."""
    report = await run(db_session, embedder, chunk_size_tokens=200)

    assert report.settings["chunk_size_tokens"] == 200
    assert report.settings["embedding_provider"] == "hashing"


async def test_metrics_move_when_retrieval_is_crippled(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The property that makes the harness an instrument rather than decoration.

    A measurement reading the same however the system is configured measures
    nothing. Cutting `top_k` to 1 must visibly cost recall on the
    multi-document question, which needs two documents to be complete.
    """
    generous = await run(db_session, embedder, retrieval_top_k=5)
    stingy = await run(db_session, embedder, retrieval_top_k=1)

    assert stingy.aggregate["recall"] < generous.aggregate["recall"]


async def test_the_eval_corpus_is_deleted_afterwards(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """A run leaving its documents behind would inflate the *next* run's recall,
    and the improvement would look real."""
    await run(db_session, embedder)

    remaining = await db_session.scalars(
        select(Organization).where(Organization.name.like("eval-%"))
    )

    assert remaining.all() == []


async def test_the_corpus_is_deleted_even_when_a_run_fails(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Cleanup sits in a `finally`, and this is why. A run that dies partway
    must not leave an eval corpus for the next run to retrieve from."""
    settings = get_settings()

    class ExplodingJudge:
        async def score(self, **_: object) -> None:
            message = "judge exploded"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="judge exploded"):
        await run_eval(
            db_session,
            embedder,
            Generator(db_session, embedder, OfflineLLM(), settings),
            ExplodingJudge(),  # type: ignore[arg-type]
            settings,
            dataset_name="handbook",
        )

    remaining = await db_session.scalars(
        select(Organization).where(Organization.name.like("eval-%"))
    )
    assert remaining.all() == []


# --------------------------------------------------------------------------
# the aggregate
# --------------------------------------------------------------------------


def test_refusal_examples_are_excluded_from_the_recall_average() -> None:
    """Otherwise a dataset could raise its own recall by adding more
    unanswerable questions — which score a free 1.0 by construction — and the
    headline number would improve while the system got worse."""
    report = EvalReport(dataset="d", generated_at="now", settings={})
    report.results = [
        ExampleResult(
            example_id="answerable",
            question="?",
            answer="a",
            relevant_document_ids=["x"],
            recall=0.0,
        ),
        ExampleResult(example_id="refusal", question="?", answer="a", refused=True),
    ]

    assert report.aggregate["recall"] == 0.0, "the refusal must not lift the average"
    assert report.aggregate["refusal_accuracy"] == 1.0


def test_an_empty_report_has_no_aggregate() -> None:
    """Rather than a set of perfect scores over zero measurements."""
    assert EvalReport(dataset="d", generated_at="now", settings={}).aggregate == {}


# --------------------------------------------------------------------------
# the regression gate
# --------------------------------------------------------------------------


def report_with(**aggregate: float) -> EvalReport:
    report = EvalReport(dataset="d", generated_at="now", settings={})
    report.results = [
        ExampleResult(
            example_id="one",
            question="?",
            answer="a",
            relevant_document_ids=["x"],
            recall=aggregate.get("recall", 1.0),
            precision=aggregate.get("precision", 1.0),
            reciprocal_rank=1.0,
        )
    ]
    return report


def test_a_drop_beyond_the_tolerance_is_a_regression() -> None:
    """The gate has to actually fire. One that never does turns "no regression"
    from a claim into a formality nobody reads."""
    regressions = compare_to_baseline(report_with(recall=0.5), {"recall": 1.0})

    assert [regression.metric for regression in regressions] == ["recall"]
    assert regressions[0].delta == pytest.approx(-0.5)


def test_a_drop_inside_the_tolerance_is_not() -> None:
    """These numbers are not perfectly stable — ties break differently, and a
    judge at temperature zero is still a model. A gate firing on a 0.001 drop
    would be muted within a week."""
    assert compare_to_baseline(report_with(recall=0.99), {"recall": 1.0}) == []


def test_an_improvement_is_never_a_regression() -> None:
    assert compare_to_baseline(report_with(recall=1.0), {"recall": 0.5}) == []


def test_a_metric_missing_from_the_baseline_is_ignored() -> None:
    """A newly added metric has no history, and refusing to run until somebody
    regenerates the baseline would make adding one a chore."""
    assert compare_to_baseline(report_with(recall=1.0), {"invented_metric": 1.0}) == []


def test_a_missing_baseline_passes_rather_than_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first run of a new dataset has nothing to regress against. Refusing
    would make creating a dataset a two-step ritual."""
    monkeypatch.setattr("app.evaluation.runner.BASELINE_ROOT", tmp_path)

    assert load_baseline("brand-new") == {}
    assert compare_to_baseline(report_with(recall=0.0), load_baseline("brand-new")) == []


def test_saving_a_baseline_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.evaluation.runner.BASELINE_ROOT", tmp_path)

    path = save_baseline(report_with(recall=0.75))

    assert json.loads(path.read_text())["aggregate"]["recall"] == pytest.approx(0.75)
    assert load_baseline("d")["recall"] == pytest.approx(0.75)
