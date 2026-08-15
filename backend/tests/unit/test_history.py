"""The conversation window (M10).

The mirror of `test_context.py`. Both modules fit text into a token budget and
they disagree about what to keep, so these tests are mostly about proving the
disagreement is real: history keeps the *newest* turns and drops the oldest,
where `assemble_context` keeps the highest-ranked and drops the tail.

The failure being guarded against is silent in production and expensive: a window
that kept the wrong end would hand the model the *opening* of a three-month
thread on every turn, which reads perfectly plausibly and answers the wrong
question.
"""

from __future__ import annotations

import pytest

from app.agents.history import HISTORY_HEADER, HistoryTurn, select_history
from app.rag.chunking import count_tokens


def turns(*contents: str) -> list[HistoryTurn]:
    """Alternating user/assistant turns, oldest first."""
    return [
        HistoryTurn(role="user" if index % 2 == 0 else "assistant", content=content)
        for index, content in enumerate(contents)
    ]


def test_an_empty_conversation_renders_nothing() -> None:
    """A first turn has no history, and must not get an empty header.

    A heading with nothing under it is a claim ("here is the conversation so
    far") that contradicts itself, and models act on headings.
    """
    selected = select_history([], budget=1000)

    assert selected.is_empty
    assert selected.text == ""
    assert selected.tokens == 0


def test_a_short_conversation_survives_whole_and_in_order() -> None:
    """Reading order is prompt order — the oldest turn first."""
    selected = select_history(turns("first", "second", "third"), budget=1000)

    assert [turn.content for turn in selected.turns] == ["first", "second", "third"]
    assert selected.dropped == 0
    assert selected.text.startswith(HISTORY_HEADER)
    assert selected.text.index("first") < selected.text.index("third")


def test_the_window_drops_the_oldest_turns_not_the_newest() -> None:
    """The whole reason this module is not `assemble_context`.

    With a budget too small for everything, the turns that survive must be the
    recent ones. Keeping the head instead would pass any test that only counted
    turns, and be catastrophic in a real conversation.
    """
    long_turns = turns(*[f"turn number {index} " + "padding " * 20 for index in range(10)])

    selected = select_history(long_turns, budget=200)

    assert selected.turns, "the budget must fit at least the newest turn"
    assert selected.dropped > 0
    kept = [turn.content for turn in selected.turns]
    assert kept == [turn.content for turn in long_turns[-len(kept) :]]
    assert "turn number 0" not in selected.text


def test_the_rendered_block_stays_inside_its_budget() -> None:
    """The bill is the point. A window that overshoots is an unbounded prompt
    with extra steps."""
    long_turns = turns(*[f"message {index} " + "words " * 30 for index in range(40)])

    selected = select_history(long_turns, budget=300)

    assert count_tokens(selected.text) <= 300


def test_a_conversation_that_keeps_growing_does_not_cost_more() -> None:
    """The failure M10 exists to prevent: cost growing with thread age.

    Ten turns and two hundred turns must produce a similarly sized prompt, or
    every long-running conversation quietly becomes the most expensive thing in
    the product.
    """
    short = select_history(
        turns(*[f"turn {index} " + "words " * 20 for index in range(10)]), budget=250
    )
    long = select_history(
        turns(*[f"turn {index} " + "words " * 20 for index in range(200)]), budget=250
    )

    assert long.tokens <= 250
    assert abs(long.tokens - short.tokens) <= 10
    assert long.dropped > short.dropped


def test_a_turn_is_collapsed_onto_one_line() -> None:
    """A real coupling, not tidiness.

    `app/llm/offline.py` finds context blocks by matching `[n]` at the start of a
    line. An assistant reply whose second line begins with a citation marker
    would be parsed as a *source* — so the next answer would be built out of the
    previous answer. One line per turn makes that unrepresentable.
    """
    selected = select_history(
        [HistoryTurn(role="assistant", content="Expenses are reimbursed.\n[1] handbook.pdf")],
        budget=1000,
    )

    body = selected.text.removeprefix(HISTORY_HEADER).strip()
    assert "\n" not in body
    assert not body.startswith("[")


def test_roles_are_labelled_for_a_reader() -> None:
    """The model reads prose, not enum values."""
    selected = select_history(turns("hello", "hi"), budget=1000)

    assert "User: hello" in selected.text
    assert "Assistant: hi" in selected.text


def test_an_unknown_role_still_renders() -> None:
    """M15 adds roles this module has never heard of. A window that dropped or
    crashed on one would lose a turn silently, which is worse than labelling it
    imperfectly."""
    selected = select_history([HistoryTurn(role="tool", content="ran a search")], budget=1000)

    assert "Tool: ran a search" in selected.text


def test_a_non_positive_budget_is_rejected() -> None:
    """The same contract as `assemble_context`. A budget of zero is a caller bug,
    and returning an empty window would hide it behind a model that suddenly has
    no memory of the conversation."""
    with pytest.raises(ValueError, match="budget must be positive"):
        select_history(turns("hello"), budget=0)
