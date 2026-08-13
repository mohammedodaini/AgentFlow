"""Chunking: the hard bound, the boundaries, and the overlap (M6).

`chunk_text` is a pure function, so it can be tested exhaustively for almost
nothing — and it should be, because everything downstream is capped by it. A
chunk that exceeds the size limit is rejected by the embedding API; a chunk
that splits a sentence cannot be retrieved by a question about either half.

The test that matters most is the invariant one: *no chunk ever exceeds
chunk_size*. It is asserted over several shapes of input rather than one,
because the interesting failures live at the seams — a paragraph exactly at the
limit, a paragraph far over it, overlap eating into the budget.
"""

from __future__ import annotations

import pytest

from app.rag.chunking import Chunk, chunk_text, count_tokens

PARAGRAPHS = "\n\n".join(
    [
        "AgentFlow ingests documents so they can be searched later.",
        "Expenses are reimbursed monthly, provided a receipt is attached.",
        "The support team answers within one business day.",
    ]
)


def _sizes(chunks: list[Chunk]) -> list[int]:
    return [chunk.token_count for chunk in chunks]


# --------------------------------------------------------------------------
# the hard bound
# --------------------------------------------------------------------------


@pytest.mark.parametrize("chunk_size", [8, 16, 32, 64, 400])
@pytest.mark.parametrize("overlap", [0, 4, 15])
def test_no_chunk_ever_exceeds_the_limit(chunk_size: int, overlap: int) -> None:
    """The invariant the whole function exists to maintain.

    Parametrised across sizes because two separate things compete with content
    for the same budget, and each was got wrong on the first attempt: the
    overlap tail, and the `\\n\\n` separator that rejoins units — which costs a
    token per join and was simply not counted. A version that respects the
    bound at `overlap=0` can still break it the moment a tail is carried.
    """
    if overlap > chunk_size // 2:
        pytest.skip("overlap above half the chunk size is refused by design")

    text = "\n\n".join(PARAGRAPHS for _ in range(6))

    chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)

    assert chunks, "a non-empty document must produce chunks"
    assert all(size <= chunk_size for size in _sizes(chunks)), _sizes(chunks)


def test_a_paragraph_larger_than_the_limit_is_split() -> None:
    """The fallback when structure cannot be honoured.

    One unbroken wall of text — a bad export, a minified table — has no
    paragraph boundary to break on. Splitting mid-sentence is ugly; exceeding
    the model's input limit is fatal, so the hard bound wins.
    """
    wall = " ".join(f"word{index}" for index in range(2000))

    chunks = chunk_text(wall, chunk_size=100, overlap=10)

    assert len(chunks) > 1
    assert all(size <= 100 for size in _sizes(chunks))


# --------------------------------------------------------------------------
# structure and overlap
# --------------------------------------------------------------------------


def test_short_text_stays_in_one_chunk() -> None:
    """Splitting a document that already fits would only fragment its meaning."""
    chunks = chunk_text(PARAGRAPHS, chunk_size=400, overlap=60)

    assert len(chunks) == 1
    assert chunks[0].content.strip() == PARAGRAPHS.strip()
    assert chunks[0].index == 0


def test_paragraph_boundaries_are_preferred_over_arbitrary_cuts() -> None:
    """With a budget that fits roughly one paragraph, chunks should break where
    the paragraphs do rather than mid-sentence."""
    chunks = chunk_text(PARAGRAPHS, chunk_size=20, overlap=0)

    assert len(chunks) >= 2
    assert any("Expenses are reimbursed monthly" in chunk.content for chunk in chunks)


def test_consecutive_chunks_share_text() -> None:
    """Overlap is insurance against the one failure a fixed boundary
    guarantees: a fact stated across a boundary lands in no chunk at all."""
    text = "\n\n".join(f"Paragraph number {index} with some filler words." for index in range(30))

    with_overlap = chunk_text(text, chunk_size=40, overlap=15)
    without_overlap = chunk_text(text, chunk_size=40, overlap=0)

    assert len(with_overlap) >= 2
    assert sum(_sizes(with_overlap)) > sum(_sizes(without_overlap)), (
        "overlap must duplicate some text — that is its cost, and its point"
    )


def test_indices_are_contiguous_and_zero_based() -> None:
    """`chunk_index` is a position, and the unique constraint on
    `(document_id, chunk_index)` depends on it being one."""
    chunks = chunk_text("\n\n".join(PARAGRAPHS for _ in range(8)), chunk_size=30, overlap=5)

    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


def test_token_count_matches_the_content() -> None:
    """Stored on the row and used by M7 to budget a context window, so a count
    that disagreed with its own text would overrun it."""
    chunks = chunk_text(PARAGRAPHS, chunk_size=25, overlap=5)

    for chunk in chunks:
        assert chunk.token_count == count_tokens(chunk.content)


def test_chunking_is_deterministic() -> None:
    """Re-ingesting an unchanged document must not reshuffle its chunks —
    otherwise every citation ever issued against it silently moves."""
    first = chunk_text(PARAGRAPHS, chunk_size=25, overlap=5)
    second = chunk_text(PARAGRAPHS, chunk_size=25, overlap=5)

    assert first == second


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\n\t\n"])
def test_empty_input_produces_no_chunks(text: str) -> None:
    """Not one empty chunk. An embedded empty string is a vector with no
    meaning that still competes in every search."""
    assert chunk_text(text, chunk_size=400, overlap=60) == []


def test_overlap_equal_to_chunk_size_is_refused() -> None:
    """The guard against a loop that never advances.

    With `overlap == chunk_size` every chunk would begin with exactly the
    tokens the previous one ended with, so the function would allocate until
    the process died. Refusing is the only safe answer.
    """
    with pytest.raises(ValueError, match="overlap"):
        chunk_text(PARAGRAPHS, chunk_size=100, overlap=100)


def test_overlap_above_half_the_chunk_is_refused() -> None:
    """A stricter bound than "must advance", and deliberately so.

    Overlap beyond half means consecutive chunks are mostly copies of each
    other: storage and embedding spend go up while retrieval gets worse,
    because near-duplicate chunks crowd each other out of the top results.
    It also guarantees there is room for real content beside a tail.
    """
    with pytest.raises(ValueError, match="chunk_size // 2"):
        chunk_text(PARAGRAPHS, chunk_size=100, overlap=51)


@pytest.mark.parametrize(
    ("chunk_size", "overlap"), [(0, 0), (-5, 0), (4, 0), (100, -1), (100, 200)]
)
def test_invalid_geometry_is_refused(chunk_size: int, overlap: int) -> None:
    with pytest.raises(ValueError, match="chunk_size|overlap"):
        chunk_text(PARAGRAPHS, chunk_size=chunk_size, overlap=overlap)
