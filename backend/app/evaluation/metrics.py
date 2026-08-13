"""Deterministic retrieval metrics. Pure functions, no model, no I/O.

Layer: evaluation. These come first in M8, before any LLM-as-judge scoring, and
the ordering is the point: they are free, instant, reproducible, and they bound
everything downstream. An answer cannot be better than the passages it was
given, so a retrieval regression is an answer regression that has not happened
yet.

They also measure different things, and using one where another belongs is the
classic evaluation mistake:

- **Recall@k** — did we find the right thing at all? The question that matters
  most for RAG, because a passage that was never retrieved cannot be cited,
  quoted or reasoned over, however good the model is.
- **Precision@k** — how much of what we sent was worth sending? Directly a cost
  and a quality number: every irrelevant chunk is tokens paid for and context
  the relevant passage has to compete with.
- **MRR** — how *high* did the right thing rank? Recall treats a hit at rank 1
  and a hit at rank 10 identically; models weight earlier context more heavily,
  so they are not identical at all.

Reporting only recall is how a retrieval system gets "improved" by raising
`top_k` until everything is recalled and every answer is worse.
"""

from __future__ import annotations

from dataclasses import dataclass


def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """What fraction of the relevant items appear in the top `k`.

    Returns 1.0 when there is nothing relevant to find. The opposite choice is
    defensible — 0.0 would say "we failed" — but a question with no relevant
    documents is one the system should *refuse*, and scoring the refusal as a
    retrieval failure would make the aggregate mean two different things at
    once. Examples like that belong in the refusal tests, not the recall
    average.
    """
    _require_positive(k)

    if not relevant:
        return 1.0

    return len(relevant & set(retrieved[:k])) / len(relevant)


def precision_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """What fraction of the top `k` were actually relevant.

    Denominated by what was *returned*, not by `k`. If only three chunks exist
    and all three are relevant, precision@5 is 1.0 — dividing by 5 would punish
    the system for a small corpus, which is not a retrieval quality it can do
    anything about.
    """
    _require_positive(k)

    window = retrieved[:k]

    if not window:
        return 0.0

    return len([item for item in window if item in relevant]) / len(window)


def reciprocal_rank(retrieved: list[str], relevant: set[str]) -> float:
    """`1 / rank` of the first relevant item, or 0.0 if none was found.

    Ranks are 1-based, because a first-place hit should score 1.0 and `1/0` is
    not a number.
    """
    for position, item in enumerate(retrieved, start=1):
        if item in relevant:
            return 1.0 / position

    return 0.0


def mrr(ranks: list[float]) -> float:
    """Mean reciprocal rank across a dataset.

    A plain mean of per-question `reciprocal_rank`. Its own function rather than
    an inline `sum(...)/len(...)` so the aggregation is named and tested —
    averaging already-averaged numbers is one of the easier ways to report a
    metric that is quietly wrong.
    """
    return sum(ranks) / len(ranks) if ranks else 0.0


def citation_accuracy(cited: list[str], supporting: set[str]) -> float:
    """What fraction of an answer's citations point at sources it was given.

    The one metric here that measures *generation* rather than retrieval, and it
    is deterministic — which is exactly why it lives beside these rather than in
    the judge. A model that cites `[3]` when the corpus only offered two sources
    produces an answer that looks perfectly sourced and is not, and unlike a
    hallucinated fact this can be checked by code.

    Returns 1.0 for an answer that cited nothing. An uncited answer is a
    *coverage* failure, not an accuracy one, and folding the two together would
    let a system score perfectly by never citing anything at all.
    """
    if not cited:
        return 1.0

    return len([item for item in cited if item in supporting]) / len(cited)


@dataclass(frozen=True)
class RetrievalMetrics:
    """One question's retrieval scores.

    Grouped so a report can carry all three per example. The aggregate is a mean
    of these, computed by the runner — the only place that knows how many
    examples there were.
    """

    recall: float
    precision: float
    reciprocal_rank: float

    @property
    def found(self) -> bool:
        """Whether anything relevant was retrieved at all.

        The most actionable single number in an eval report: an example scoring
        0 here cannot be rescued by a better prompt, a better model, or a larger
        context budget. Only retrieval can fix it.
        """
        return self.reciprocal_rank > 0.0


def score_retrieval(retrieved: list[str], relevant: set[str], k: int) -> RetrievalMetrics:
    """All three retrieval metrics for one question."""
    return RetrievalMetrics(
        recall=recall_at_k(retrieved, relevant, k),
        precision=precision_at_k(retrieved, relevant, k),
        reciprocal_rank=reciprocal_rank(retrieved[:k], relevant),
    )


def _require_positive(k: int) -> None:
    if k <= 0:
        message = f"k must be positive, got {k}"
        raise ValueError(message)
