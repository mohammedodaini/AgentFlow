"""The eval runner: dataset → system → metrics → report → verdict.

Layer: evaluation. **The regression gate.** `docs/agents.md` is explicit that no
prompt or retrieval change ships without this confirming no regression on the
golden set, and that promise is only as good as this module's willingness to
report a failure.

What a run actually does
------------------------
1. Build a throwaway organization and ingest the dataset's own corpus into it —
   real chunking, real embeddings, real pgvector. Not fixtures: the thing being
   measured is the pipeline, and a run against pre-placed chunks would measure
   the fixtures.
2. Ask every question through the real `Generator`, the same class `/ask` uses.
3. Score retrieval deterministically; score the answer with the judge.
4. Aggregate, compare against a stored baseline, and say pass or fail.
5. Delete the throwaway organization, whatever happened.

Why the corpus is ingested rather than mocked
---------------------------------------------
Because every number here is meant to move when chunk geometry moves. That is
the entire purpose of M8: `chunk_size_tokens`, `chunk_overlap_tokens`,
`retrieval_top_k` and `context_token_budget` are all documented in `config.py`
as "a starting point, not a finding", and this is the instrument that turns them
into findings. An eval that skipped ingestion could not see a chunking change at
all.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.evaluation import metrics
from app.evaluation.datasets import GoldenDataset, GoldenExample, load_dataset
from app.evaluation.judge import Judge, JudgeScore
from app.llm.offline import NO_CONTEXT_ANSWER
from app.models.document import Document, DocumentSource, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.organization import Organization
from app.rag.chunking import chunk_text
from app.rag.embeddings import EmbeddingProvider
from app.rag.generation import Generator

logger = structlog.get_logger(__name__)

BASELINE_ROOT = Path(__file__).parent / "baselines"
"""Committed to the repository, so a regression is visible in a diff and a
deliberate improvement is a reviewable change to a number."""


@dataclass
class ExampleResult:
    """One question's full result. Kept per-example rather than only aggregated.

    An aggregate says the system got worse; only the per-example rows say
    *which* questions got worse, which is the difference between a number and
    something somebody can fix. Reports stay small — a golden set is measured in
    tens of questions, not thousands.
    """

    example_id: str
    question: str
    answer: str

    retrieved_document_ids: list[str] = field(default_factory=list)
    relevant_document_ids: list[str] = field(default_factory=list)

    recall: float = 0.0
    precision: float = 0.0
    reciprocal_rank: float = 0.0
    citation_accuracy: float = 1.0

    contains_expected: bool = True
    """Whether every `answer_contains` substring is present. Deterministic and
    free, and it catches an answer that is simply wrong before any judge is
    asked for an opinion about it."""

    refused: bool = False

    refusal_correct: bool = True
    """Whether refusing — or not — was the right call. The most important
    boolean in the report: a system that answers everything looks excellent on
    answerable questions and invents the rest."""

    faithfulness: int = 0
    relevance: int = 0
    completeness: int = 0
    judge_notes: str = ""


@dataclass
class EvalReport:
    """Everything one run produced, plus the aggregate the gate compares."""

    dataset: str
    generated_at: str

    settings: dict[str, object]
    """The knobs this run used. Without them a report is a number with no
    experiment attached — and the point of M8 is comparing runs that differ by
    exactly one setting."""

    results: list[ExampleResult] = field(default_factory=list)

    @property
    def aggregate(self) -> dict[str, float]:
        """The headline numbers, and the ones the regression gate reads."""
        if not self.results:
            return {}

        answerable = [result for result in self.results if result.relevant_document_ids]
        refusals = [result for result in self.results if not result.relevant_document_ids]

        return {
            # Averaged over answerable questions only. Including the refusal
            # examples — which score a free 1.0 by construction — would let a
            # dataset raise its own recall by adding more unanswerable
            # questions, which is precisely backwards.
            "recall": _mean([result.recall for result in answerable]),
            "precision": _mean([result.precision for result in answerable]),
            "mrr": metrics.mrr([result.reciprocal_rank for result in answerable]),
            "answer_match": _mean([float(result.contains_expected) for result in answerable]),
            "refusal_accuracy": _mean([float(result.refusal_correct) for result in refusals]),
            "citation_accuracy": _mean([result.citation_accuracy for result in self.results]),
            "faithfulness": _mean([float(result.faithfulness) for result in self.results]),
            "relevance": _mean([float(result.relevance) for result in self.results]),
            "completeness": _mean([float(result.completeness) for result in self.results]),
        }

    def to_json(self) -> str:
        return json.dumps(
            {
                "dataset": self.dataset,
                "generated_at": self.generated_at,
                "settings": self.settings,
                "aggregate": self.aggregate,
                "examples": [asdict(result) for result in self.results],
            },
            indent=2,
            sort_keys=True,
        )


@dataclass(frozen=True)
class Regression:
    """One metric that fell further than the tolerance allows."""

    metric: str
    baseline: float
    current: float

    @property
    def delta(self) -> float:
        return self.current - self.baseline


async def run_eval(
    session: AsyncSession,
    embedder: EmbeddingProvider,
    generator: Generator,
    judge: Judge,
    settings: Settings,
    *,
    dataset_name: str = "handbook",
) -> EvalReport:
    """Run `dataset_name` end to end and return its report.

    The `Generator` is passed in rather than built here so a caller can evaluate
    a *variant* — a different prompt, a different provider — without this
    function growing a parameter per knob.
    """
    dataset = load_dataset(dataset_name)
    organization = Organization(
        name=f"eval-{dataset.name}", slug=f"eval-{dataset.name}-{uuid.uuid4().hex[:8]}"
    )
    session.add(organization)
    await session.flush()

    try:
        document_ids = await _ingest(session, embedder, organization.id, dataset, settings)

        report = EvalReport(
            dataset=dataset.name,
            generated_at=datetime.now(UTC).isoformat(),
            settings={
                "chunk_size_tokens": settings.chunk_size_tokens,
                "chunk_overlap_tokens": settings.chunk_overlap_tokens,
                "retrieval_top_k": settings.retrieval_top_k,
                "context_token_budget": settings.context_token_budget,
                "embedding_provider": settings.embedding_provider,
                "llm_provider": settings.llm_provider,
                "llm_model": settings.llm_model,
            },
        )

        for example in dataset.examples:
            report.results.append(
                await _evaluate(generator, judge, organization.id, example, document_ids, settings)
            )

        logger.info("eval.completed", dataset=dataset.name, **report.aggregate)
        return report
    finally:
        # Always, including on failure. A run that dies partway must not leave
        # an eval corpus behind for the next run to retrieve from — which would
        # show up as mysteriously improved recall.
        await session.delete(organization)
        await session.flush()


async def _ingest(
    session: AsyncSession,
    embedder: EmbeddingProvider,
    organization_id: uuid.UUID,
    dataset: GoldenDataset,
    settings: Settings,
) -> dict[uuid.UUID, str]:
    """Chunk and embed the dataset's corpus. Returns database id → dataset id.

    That mapping is the whole reason this returns anything. Metrics are keyed on
    the dataset's stable labels ("expenses"), while retrieval returns database
    UUIDs generated fresh on every run — without the translation, every example
    would score zero recall and look like total retrieval failure.

    Written directly rather than through `DocumentService` and the arq worker.
    That path is already covered by `tests/integration/test_ingestion_task.py`;
    routing an eval through a queue would add a worker process to every run and
    measure nothing extra.
    """
    mapping: dict[uuid.UUID, str] = {}

    for golden in dataset.documents:
        document = Document(
            organization_id=organization_id,
            title=golden.title,
            source=DocumentSource.UPLOAD,
            mime_type="text/plain",
            storage_uri=f"eval/{dataset.name}/{golden.id}",
            byte_size=len(golden.content.encode()),
            status=DocumentStatus.READY,
        )
        session.add(document)
        await session.flush()

        chunks = chunk_text(
            golden.content,
            chunk_size=settings.chunk_size_tokens,
            overlap=settings.chunk_overlap_tokens,
        )
        vectors = await embedder.embed_documents([chunk.content for chunk in chunks])

        session.add_all(
            DocumentChunk(
                document_id=document.id,
                chunk_index=chunk.index,
                content=chunk.content,
                token_count=chunk.token_count,
                embedding=vector,
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        )
        await session.flush()

        mapping[document.id] = golden.id

    return mapping


async def _evaluate(
    generator: Generator,
    judge: Judge,
    organization_id: uuid.UUID,
    example: GoldenExample,
    document_ids: dict[uuid.UUID, str],
    settings: Settings,
) -> ExampleResult:
    """Ask one question and score everything about the answer."""
    answer = await generator.answer(organization_id, example.question)

    retrieved = [
        document_ids[uuid.UUID(source.document_id)]
        for source in answer.sources
        if uuid.UUID(source.document_id) in document_ids
    ]
    # De-duplicated, preserving order: several chunks of one document are one
    # retrieval hit, and counting them separately would inflate recall for a
    # long document and deflate precision for a short one.
    ordered = list(dict.fromkeys(retrieved))

    scores = metrics.score_retrieval(
        ordered, example.relevant_document_ids, settings.retrieval_top_k
    )

    refused = answer.is_refusal or answer.text.strip() == NO_CONTEXT_ANSWER
    sources_text = "\n\n".join(
        f"[{source.number}] {source.document_title}" for source in answer.sources
    )

    verdict = await _judge(judge, example, answer.text, sources_text, refused)

    return ExampleResult(
        example_id=example.id,
        question=example.question,
        answer=answer.text,
        retrieved_document_ids=ordered,
        relevant_document_ids=sorted(example.relevant_document_ids),
        recall=scores.recall,
        precision=scores.precision,
        reciprocal_rank=scores.reciprocal_rank,
        citation_accuracy=metrics.citation_accuracy(
            _cited_markers(answer.text), {str(source.number) for source in answer.sources}
        ),
        contains_expected=all(
            needle.lower() in answer.text.lower() for needle in example.answer_contains
        ),
        refused=refused,
        refusal_correct=refused == example.expect_refusal,
        faithfulness=verdict.faithfulness,
        relevance=verdict.relevance,
        completeness=verdict.completeness,
        judge_notes=verdict.notes,
    )


async def _judge(
    judge: Judge, example: GoldenExample, answer: str, sources: str, refused: bool
) -> JudgeScore:
    """Score the answer, short-circuiting a correct refusal.

    A correct refusal has no sources to be faithful to and no reference content
    to be complete against, so any judge — model or heuristic — would score it
    near zero for doing exactly the right thing. Awarding full marks here is not
    generosity; it is the only way the aggregate stays readable once a dataset
    contains the refusal examples it should.
    """
    if refused and example.expect_refusal:
        return JudgeScore(5, 5, 5, notes="correct refusal")

    return await judge.score(
        question=example.question,
        answer=answer,
        sources=sources,
        reference=example.reference_answer,
    )


def _cited_markers(answer: str) -> list[str]:
    """Every `[n]` the answer used."""
    return [part.split("]")[0] for part in answer.split("[")[1:] if part.split("]")[0].isdigit()]


def compare_to_baseline(
    report: EvalReport, baseline: dict[str, float], *, tolerance: float = 0.02
) -> list[Regression]:
    """Which metrics fell further than `tolerance` below the baseline.

    A tolerance rather than an exact match, because these numbers are not
    perfectly stable: embedding providers change, ties in a ranking break
    differently, and a judge at temperature zero is still a model. A gate that
    fired on a 0.001 drop would be muted within a week, and a muted gate is no
    gate at all.

    Two percentage points is a guess, and a *deliberately documented* one. It
    should be tightened once several runs show how much these numbers move on
    their own.
    """
    current = report.aggregate

    return [
        Regression(metric=name, baseline=value, current=current[name])
        for name, value in baseline.items()
        if name in current and current[name] < value - tolerance
    ]


def load_baseline(dataset_name: str) -> dict[str, float]:
    """The stored aggregate for `dataset_name`, or empty if none exists.

    An absent baseline is not an error: the first run of a new dataset has
    nothing to regress against, and refusing to run would make creating a
    dataset a two-step ritual. It returns an empty dict, `compare_to_baseline`
    finds no regressions, and the run passes — after which an operator writes
    the baseline deliberately.
    """
    path = BASELINE_ROOT / f"{dataset_name}.json"

    if not path.is_file():
        return {}

    stored = json.loads(path.read_text(encoding="utf-8"))
    aggregate: dict[str, float] = stored.get("aggregate", {})
    return aggregate


def save_baseline(report: EvalReport) -> Path:
    """Write this run's aggregate as the new baseline.

    Never automatic. Overwriting a baseline with the current run makes every
    future comparison pass by construction, which is how a regression gate
    becomes a formality — so this is only ever called by an operator who has
    read the report and decided the change is an improvement.
    """
    BASELINE_ROOT.mkdir(parents=True, exist_ok=True)
    path = BASELINE_ROOT / f"{report.dataset}.json"
    path.write_text(
        json.dumps(
            {
                "dataset": report.dataset,
                "generated_at": report.generated_at,
                "settings": report.settings,
                "aggregate": report.aggregate,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
