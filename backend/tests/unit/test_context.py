"""Context assembly: the budget, the ordering, and the citation map (M7).

A pure function, so it is tested exhaustively for almost nothing. Two failures
here are expensive and neither announces itself.

**An unbounded context** costs money on every question, silently, and shows up
only on a bill.

**A citation map that disagrees with the prompt** makes every citation in the
product wrong while every test of the plumbing still passes: the answer says
`[2]`, the API returns a `[2]` pointing at a different chunk, and nothing
raises.
"""

from __future__ import annotations

import uuid

import pytest

from app.rag.chunking import count_tokens
from app.rag.context import SOURCE_TEMPLATE, assemble_context
from app.repositories.chunk_repository import ScoredChunk

EXPENSES = "Expenses are reimbursed monthly, provided a receipt is attached."
HOLIDAY = "Holiday requests must be approved by a line manager two weeks ahead."
PLANTS = "The office plants are watered every Tuesday by the facilities team."


def chunk(content: str, *, title: str = "handbook.pdf", score: float = 0.5) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_title=title,
        chunk_index=0,
        content=content,
        token_count=count_tokens(content),
        score=score,
    )


# --------------------------------------------------------------------------
# the citation map — the property everything else depends on
# --------------------------------------------------------------------------


def test_source_numbers_match_the_markers_in_the_prompt() -> None:
    """The invariant that makes a citation mean anything.

    The model sees `[1]`, `[2]`, `[3]` and reproduces one of them; the API
    returns citations carrying those same numbers. If the two disagree, every
    citation in the product points at the wrong passage — and nothing raises,
    because both halves are individually well-formed.
    """
    context = assemble_context([chunk(EXPENSES), chunk(HOLIDAY), chunk(PLANTS)], budget=1000)

    assert [source.number for source in context.sources] == [1, 2, 3]

    for source in context.sources:
        marker = f"[{source.number}]"
        assert marker in context.text
        # The block following this marker must belong to the chunk it names.
        block = context.text.split(marker, 1)[1]
        assert block.startswith(f" {source.document_title}")


def test_numbering_starts_at_one() -> None:
    """`[0]` reads as a footnote error to every human who has ever seen a
    citation, and models trained on human text write `[1]` regardless."""
    context = assemble_context([chunk(EXPENSES)], budget=1000)

    assert context.sources[0].number == 1


def test_each_source_carries_what_a_client_needs_to_highlight_it() -> None:
    """Naming the document is not a citation for a 200-page handbook. The chunk
    id and index are what let a UI point at the passage itself."""
    result = chunk(EXPENSES, title="policies.pdf", score=0.75)

    source = assemble_context([result], budget=1000).sources[0]

    assert source.chunk_id == str(result.chunk_id)
    assert source.document_id == str(result.document_id)
    assert source.document_title == "policies.pdf"
    assert source.chunk_index == result.chunk_index
    assert source.score == pytest.approx(0.75)


# --------------------------------------------------------------------------
# the budget
# --------------------------------------------------------------------------


@pytest.mark.parametrize("budget", [20, 30, 40, 60, 1000])
def test_the_assembled_context_never_exceeds_the_budget(budget: int) -> None:
    """A hard bound, measured on the finished text rather than on the parts.

    Token counts are not additive under concatenation — BPE merges across a
    join — which produced a real off-by-one bug in `chunking.py` at M6. The
    running total decides what fits; this asserts what actually got sent.
    """
    results = [chunk(EXPENSES), chunk(HOLIDAY), chunk(PLANTS)]

    context = assemble_context(results, budget=budget)

    assert count_tokens(context.text) <= budget, context.text


def test_the_separator_is_counted_against_the_budget() -> None:
    """Anything that contributes characters to the output is charged for.

    Exactly the mistake the chunker made at M6: a budget that holds until
    something joins two pieces together, then quietly exceeds it by a token per
    join.
    """
    exact = count_tokens(SOURCE_TEMPLATE.format(number=1, title="handbook.pdf", content=EXPENSES))
    exact += count_tokens(SOURCE_TEMPLATE.format(number=2, title="handbook.pdf", content=HOLIDAY))

    # Both blocks fit the budget on their own; only the separator pushes it over.
    context = assemble_context([chunk(EXPENSES), chunk(HOLIDAY)], budget=exact)

    assert len(context.sources) == 1
    assert context.dropped == 1


def test_truncation_drops_the_weakest_not_the_strongest() -> None:
    """Filling greedily from the top means the budget cuts the tail — the half
    most likely to be noise. Dropping the best match to fit two marginal ones
    would be worse than returning fewer sources."""
    best = chunk(EXPENSES, score=0.9)
    worst = chunk(PLANTS, score=0.1)

    context = assemble_context([best, worst], budget=count_tokens(EXPENSES) + 12)

    assert [source.score for source in context.sources] == [pytest.approx(0.9)]
    assert EXPENSES in context.text
    assert PLANTS not in context.text


def test_a_later_chunk_can_still_fit_after_one_that_did_not() -> None:
    """No `break` on the first chunk that does not fit.

    A long chunk followed by a short one should not cost us the short one — the
    budget could afford it, and stopping early throws away evidence for nothing.
    Numbering stays contiguous because it is derived from the sources kept.
    """
    long_chunk = chunk(" ".join(f"word{index}" for index in range(400)))

    context = assemble_context([long_chunk, chunk(EXPENSES)], budget=count_tokens(EXPENSES) + 12)

    assert len(context.sources) == 1
    assert context.sources[0].number == 1, "numbering must stay contiguous"
    assert EXPENSES in context.text
    assert context.dropped == 1


def test_dropped_sources_are_reported() -> None:
    """The signal that the budget is binding. Invisible in the answer, and the
    number an operator needs before deciding whether to raise it."""
    results = [chunk(EXPENSES), chunk(HOLIDAY), chunk(PLANTS)]

    context = assemble_context(results, budget=count_tokens(EXPENSES) + 12)

    assert context.dropped == 2
    assert len(context.sources) == 1


def test_a_chunk_is_skipped_whole_never_truncated() -> None:
    """Half a passage can invert what it says.

    A cut before "unless the invoice exceeds £500" turns a rule into its
    opposite, and a citation pointing at a sentence the model never saw the end
    of is worse than no citation.
    """
    rule = "Expenses are reimbursed in full unless the invoice exceeds five hundred pounds."

    context = assemble_context([chunk(rule)], budget=count_tokens(rule) // 2)

    assert context.is_empty
    assert context.text == ""


# --------------------------------------------------------------------------
# refusals and edges
# --------------------------------------------------------------------------


def test_no_results_produce_an_empty_context() -> None:
    """Which is what makes the generator refuse instead of calling the model
    with an empty context block — the worst failure a RAG system has."""
    context = assemble_context([], budget=1000)

    assert context.is_empty
    assert context.text == ""
    assert context.tokens == 0
    assert context.dropped == 0


@pytest.mark.parametrize("budget", [0, -1])
def test_a_non_positive_budget_is_refused(budget: int) -> None:
    """Zero would answer every question with no context at all, which reads as
    "your documents say nothing" for every question ever asked."""
    with pytest.raises(ValueError, match="budget"):
        assemble_context([chunk(EXPENSES)], budget=budget)


def test_the_document_title_travels_with_the_content() -> None:
    """Provenance changes how a passage should be read: "handbook.pdf" and
    "draft-proposal-v3.pdf" deserve different confidence, and only the title
    carries that."""
    context = assemble_context([chunk(EXPENSES, title="draft-proposal-v3.pdf")], budget=1000)

    assert "draft-proposal-v3.pdf" in context.text
