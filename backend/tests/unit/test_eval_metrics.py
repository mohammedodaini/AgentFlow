"""Retrieval metrics and the golden-set loader (M8).

Pure functions and file parsing, so these are exhaustive and instant. They earn
their place because an evaluation harness is the one piece of software whose
bugs are *invisible by construction*: a metric that is quietly wrong reports a
number, everybody believes it, and decisions get made on it for months.

The loader tests are mostly about refusals, for the same reason. A dataset that
silently loads as empty produces a perfect score from zero measurements — and in
CI a green run over nothing looks exactly like a green run over everything.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.evaluation import metrics
from app.evaluation.datasets import DatasetError, load_dataset

# --------------------------------------------------------------------------
# recall, precision, MRR
# --------------------------------------------------------------------------


def test_recall_counts_relevant_items_found() -> None:
    assert metrics.recall_at_k(["a", "b", "c"], {"a", "c"}, k=3) == 1.0
    assert metrics.recall_at_k(["a", "b", "c"], {"a", "z"}, k=3) == 0.5
    assert metrics.recall_at_k(["a", "b", "c"], {"z"}, k=3) == 0.0


def test_recall_only_looks_at_the_top_k() -> None:
    """The whole point of `@k`: a hit at rank 9 is not a hit if the system only
    sends five chunks to the model."""
    assert metrics.recall_at_k(["a", "b", "c"], {"c"}, k=2) == 0.0
    assert metrics.recall_at_k(["a", "b", "c"], {"c"}, k=3) == 1.0


def test_nothing_relevant_scores_a_full_recall() -> None:
    """A deliberate choice, and the opposite is defensible.

    Questions with no relevant documents are ones the system should *refuse*.
    Scoring a correct refusal as a retrieval failure would make the aggregate
    mean two things at once — so those examples are measured by
    `refusal_accuracy` instead, and excluded from the recall average entirely.
    """
    assert metrics.recall_at_k(["a", "b"], set(), k=2) == 1.0


def test_precision_is_denominated_by_what_was_returned() -> None:
    """Not by `k`. If only three chunks exist and all three are relevant,
    precision@5 is 1.0 — dividing by 5 would punish the system for a small
    corpus, which is not a retrieval quality it can do anything about."""
    assert metrics.precision_at_k(["a", "b", "c"], {"a", "b", "c"}, k=5) == 1.0
    assert metrics.precision_at_k(["a", "b", "c", "d"], {"a"}, k=4) == 0.25
    assert metrics.precision_at_k([], {"a"}, k=5) == 0.0


def test_reciprocal_rank_rewards_finding_it_early() -> None:
    """Recall treats a hit at rank 1 and rank 10 identically. Models do not —
    they weight earlier context more heavily — so MRR is the metric that
    notices a reranking change."""
    assert metrics.reciprocal_rank(["a", "b", "c"], {"a"}) == 1.0
    assert metrics.reciprocal_rank(["a", "b", "c"], {"b"}) == 0.5
    assert metrics.reciprocal_rank(["a", "b", "c"], {"z"}) == 0.0


def test_mrr_averages_reciprocal_ranks() -> None:
    assert metrics.mrr([1.0, 0.5, 0.0]) == pytest.approx(0.5)
    assert metrics.mrr([]) == 0.0


@pytest.mark.parametrize("k", [0, -1])
def test_a_non_positive_k_is_refused(k: int) -> None:
    """`k=0` would score every system 0.0 recall forever, which reads as a
    catastrophic regression rather than as a broken harness."""
    with pytest.raises(ValueError, match="k must be positive"):
        metrics.recall_at_k(["a"], {"a"}, k=k)

    with pytest.raises(ValueError, match="k must be positive"):
        metrics.precision_at_k(["a"], {"a"}, k=k)


# --------------------------------------------------------------------------
# citation accuracy
# --------------------------------------------------------------------------


def test_citation_accuracy_catches_a_marker_with_no_source() -> None:
    """A model citing `[3]` when it was given two sources produces an answer
    that looks perfectly sourced and is not — and unlike a hallucinated fact,
    code can check this one."""
    assert metrics.citation_accuracy(["1", "2"], {"1", "2"}) == 1.0
    assert metrics.citation_accuracy(["1", "3"], {"1", "2"}) == 0.5
    assert metrics.citation_accuracy(["9"], {"1"}) == 0.0


def test_an_uncited_answer_is_not_an_accuracy_failure() -> None:
    """It is a *coverage* failure, measured separately. Folding the two together
    would let a system score perfectly by never citing anything."""
    assert metrics.citation_accuracy([], {"1"}) == 1.0


# --------------------------------------------------------------------------
# the grouped scorer
# --------------------------------------------------------------------------


def test_score_retrieval_reports_all_three_and_whether_anything_was_found() -> None:
    """`found` is the most actionable flag in a report: an example scoring 0
    cannot be rescued by a better prompt or a larger context budget. Only
    retrieval can fix it."""
    hit = metrics.score_retrieval(["a", "b"], {"b"}, k=5)
    miss = metrics.score_retrieval(["a", "b"], {"z"}, k=5)

    assert hit.recall == 1.0
    assert hit.reciprocal_rank == 0.5
    assert hit.found

    assert miss.recall == 0.0
    assert not miss.found


# --------------------------------------------------------------------------
# the shipped golden set
# --------------------------------------------------------------------------


def test_the_shipped_dataset_loads_and_has_refusal_examples() -> None:
    """Refusal examples are the ones most often left out of a golden set, and
    the ones that catch the worst failure: a system that answers everything
    scores wonderfully on the questions its corpus covers and invents the
    rest."""
    dataset = load_dataset("handbook")

    assert dataset.examples
    assert dataset.documents
    assert [example for example in dataset.examples if example.expect_refusal], (
        "a golden set with no unanswerable questions cannot detect invention"
    )


def test_every_example_references_a_document_in_the_corpus() -> None:
    """A typo here scores 0 recall and looks exactly like a retrieval
    regression — which is why the loader validates instead of trusting."""
    dataset = load_dataset("handbook")
    known = {document.id for document in dataset.documents}

    for example in dataset.examples:
        assert example.relevant_document_ids <= known, example.id


def test_examples_name_documents_not_chunks() -> None:
    """The decision the dataset format turns on.

    Chunk ids do not exist until ingestion and depend on `chunk_size_tokens` and
    `chunk_overlap_tokens` — the settings M8 exists to tune. A golden set keyed
    on them would need regenerating on every change to the variable under test,
    which makes it a mirror rather than a measurement.
    """
    assert load_dataset("handbook").document("expenses").title.endswith(".md")


def test_an_unknown_dataset_names_the_ones_that_exist() -> None:
    with pytest.raises(DatasetError, match="handbook"):
        load_dataset("nonexistent")


def test_looking_up_an_unknown_document_raises() -> None:
    with pytest.raises(DatasetError, match="no document"):
        load_dataset("handbook").document("ghost")


# --------------------------------------------------------------------------
# malformed datasets — every one of these fails silently if not caught here
# --------------------------------------------------------------------------


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the loader at a temporary directory, and clear its cache.

    `load_dataset` is `lru_cache`d for the life of the process, which is right
    in production and would otherwise make every test below read the first
    one's file.
    """
    monkeypatch.setattr("app.evaluation.datasets.DATA_ROOT", tmp_path)
    load_dataset.cache_clear()
    yield tmp_path
    load_dataset.cache_clear()


def write_dataset(root: Path, name: str, payload: dict[str, object]) -> None:
    (root / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_an_empty_dataset_is_refused(data_root: Path) -> None:
    """The most dangerous malformed dataset there is.

    A run over zero examples scores a perfect 1.0 on everything, and in CI that
    is indistinguishable from a run over the whole golden set.
    """
    write_dataset(data_root, "empty", {"documents": [{"id": "a", "title": "a", "content": "x"}]})

    with pytest.raises(DatasetError, match="no examples"):
        load_dataset("empty")


def test_a_dataset_with_no_corpus_is_refused(data_root: Path) -> None:
    write_dataset(data_root, "nocorpus", {"examples": [{"id": "one", "question": "what?"}]})

    with pytest.raises(DatasetError, match="no documents"):
        load_dataset("nocorpus")


def test_duplicate_example_ids_are_refused(data_root: Path) -> None:
    """They overwrite each other in a report keyed by id, so one silently
    disappears from the results a human reads."""
    write_dataset(
        data_root,
        "dupes",
        {
            "documents": [{"id": "a", "title": "a", "content": "x"}],
            "examples": [{"id": "same", "question": "one?"}, {"id": "same", "question": "two?"}],
        },
    )

    with pytest.raises(DatasetError, match="duplicate example ids"):
        load_dataset("dupes")


def test_an_example_referencing_a_missing_document_is_refused(data_root: Path) -> None:
    write_dataset(
        data_root,
        "dangling",
        {
            "documents": [{"id": "a", "title": "a", "content": "x"}],
            "examples": [{"id": "one", "question": "?", "relevant_document_ids": ["ghost"]}],
        },
    )

    with pytest.raises(DatasetError, match="not in the corpus"):
        load_dataset("dangling")


def test_expecting_a_refusal_while_naming_sources_is_refused(data_root: Path) -> None:
    """A contradiction the metrics cannot represent: the example claims the
    corpus answers it *and* that it should not be answered."""
    write_dataset(
        data_root,
        "contradiction",
        {
            "documents": [{"id": "a", "title": "a", "content": "x"}],
            "examples": [
                {
                    "id": "one",
                    "question": "?",
                    "relevant_document_ids": ["a"],
                    "expect_refusal": True,
                }
            ],
        },
    )

    with pytest.raises(DatasetError, match="expects a refusal"):
        load_dataset("contradiction")


def test_malformed_json_names_the_dataset(data_root: Path) -> None:
    (data_root / "broken.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(DatasetError, match="not valid JSON"):
        load_dataset("broken")
