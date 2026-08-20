"""`python -m app.evaluation` — run the golden set and report a verdict.

Layer: evaluation (entry point). What `make eval` and CI actually invoke.

**The exit code is the product.** Everything else here — the table, the report
file — is for a human reading the output. The exit code is what makes this a
gate rather than a script somebody runs when they remember to, and it is the
only reason `docs/agents.md` can say "no prompt change ships without the
evaluation harness confirming no regression".
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.agents.supervisor.tools import RuleRouter
from app.core.config import get_settings
from app.db.session import create_engine, create_session_factory
from app.evaluation.judge import create_judge
from app.evaluation.routing_runner import RoutingReport, run_routing_eval
from app.evaluation.runner import (
    EvalReport,
    Regression,
    compare_to_baseline,
    load_baseline,
    run_eval,
    save_baseline,
)
from app.llm import create_llm
from app.rag.embeddings import create_embedder
from app.rag.generation import Generator

REPORT_ROOT = Path("var/eval")
"""Under `var/`, which is gitignored. Reports are run artefacts; only the
*baseline* is committed, because that is the number under review."""


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.evaluation", description=__doc__)
    parser.add_argument("--dataset", default="handbook", help="dataset name under data/")
    parser.add_argument(
        "--routing-only",
        action="store_true",
        help="score routing and skip the RAG set (no database or model needed)",
    )
    parser.add_argument(
        "--save-baseline",
        action="store_true",
        help="overwrite the stored baseline with this run (only after reading the report)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.02,
        help="how far a metric may fall before it counts as a regression",
    )
    arguments = parser.parse_args()

    # Routing first, and deliberately: it needs no database, no ingestion and no
    # model, so it takes milliseconds. A run that is going to fail on routing
    # should say so before spending a minute embedding a corpus.
    routing = _run_routing(save=arguments.save_baseline, tolerance=arguments.tolerance)

    if arguments.routing_only:
        return routing

    rag = asyncio.run(
        _run(arguments.dataset, save=arguments.save_baseline, tolerance=arguments.tolerance)
    )

    # Both, not the first failure: an operator fixing a regression wants to know
    # everything that broke, not to rediscover the second problem after fixing
    # the first.
    return max(routing, rag)


def _run_routing(*, save: bool, tolerance: float) -> int:
    """Score the supervisor's router, and the single-agent control beside it."""
    report = run_routing_eval(RuleRouter())
    path = _write_routing_report(report)
    regressions = compare_to_baseline(report, load_baseline(report.dataset), tolerance=tolerance)

    print(f"\n{report.dataset}: {len(report.results)} examples")
    print(f"settings: {report.settings}\n")

    for name, value in report.aggregate.items():
        print(f"  {name:<24} {value:.3f}")

    if report.failures:
        print(f"\n{len(report.failures)} example(s) routed wrongly:")
        for result in report.failures:
            print(
                f"  {result.id:<32} expected {result.expected_agent}"
                f"{result.expected_plan or ''}, got {result.actual_agent}{result.actual_plan}"
            )

    print(f"\nreport: {path}")

    if regressions:
        print(f"\nREGRESSION — routing fell more than {tolerance:.0%} below baseline:")
        for regression in regressions:
            print(
                f"  {regression.metric:<24} {regression.baseline:.3f} → "
                f"{regression.current:.3f}  ({regression.delta:+.3f})"
            )
    else:
        print("\nno routing regression against baseline")

    if save:
        print(f"baseline updated: {save_baseline(report)}")

    return 1 if regressions else 0


def _write_routing_report(report: RoutingReport) -> Path:
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    path = REPORT_ROOT / f"{report.dataset}-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    path.write_text(report.to_json() + "\n", encoding="utf-8")
    return path


async def _run(dataset: str, *, save: bool, tolerance: float) -> int:
    settings = get_settings()
    engine = create_engine(settings)

    try:
        session_factory = create_session_factory(engine)
        embedder = create_embedder(settings)
        llm = create_llm(settings)

        async with session_factory() as session:
            report = await run_eval(
                session,
                embedder,
                Generator(session, embedder, llm, settings),
                create_judge(settings, llm),
                settings,
                dataset_name=dataset,
            )
            # The run writes an organization, its documents and its chunks, then
            # deletes them again. Committing that deletion is what stops an
            # aborted run leaving an eval corpus behind in the database.
            await session.commit()
    finally:
        await engine.dispose()

    path = _write_report(report)
    regressions = compare_to_baseline(report, load_baseline(dataset), tolerance=tolerance)

    _print(report, regressions, path, tolerance)

    if save:
        print(f"\nbaseline updated: {save_baseline(report)}")

    return 1 if regressions else 0


def _write_report(report: EvalReport) -> Path:
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    path = REPORT_ROOT / f"{report.dataset}-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    path.write_text(report.to_json() + "\n", encoding="utf-8")
    return path


def _print(report: EvalReport, regressions: list[Regression], path: Path, tolerance: float) -> None:
    """Print the aggregate, then every example that failed something.

    Failures only, not all fifteen rows. A report that lists everything is a
    report nobody reads to the end, and the rows that matter are the ones
    somebody has to act on.
    """
    print(f"\n{report.dataset}: {len(report.results)} examples")
    print(f"settings: {report.settings}\n")

    for name, value in report.aggregate.items():
        print(f"  {name:<20} {value:.3f}")

    failures = [
        result
        for result in report.results
        if not result.refusal_correct or not result.contains_expected or result.recall < 1.0
    ]

    if failures:
        print(f"\n{len(failures)} example(s) needing attention:")
        for result in failures:
            reasons = []
            if not result.refusal_correct:
                reasons.append("refused" if result.refused else "should have refused")
            if not result.contains_expected:
                reasons.append("answer missing expected text")
            if result.recall < 1.0:
                reasons.append(f"recall {result.recall:.2f}")
            print(f"  {result.example_id:<28} {', '.join(reasons)}")

    print(f"\nreport: {path}")

    if regressions:
        print(f"\nREGRESSION — metrics fell more than {tolerance:.0%} below baseline:")
        for regression in regressions:
            print(
                f"  {regression.metric:<20} {regression.baseline:.3f} → "
                f"{regression.current:.3f}  ({regression.delta:+.3f})"
            )
    else:
        print("\nno regression against baseline")


if __name__ == "__main__":
    sys.exit(main())
