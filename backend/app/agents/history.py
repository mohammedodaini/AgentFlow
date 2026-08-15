"""Conversation turns → a bounded prompt block.

Layer: agents. A pure function, like `app/rag/context.py`, and deliberately its
mirror image. Both fit text into a token budget; they disagree about what to
keep, and the disagreement is the whole lesson of this module.

    context.py   ranks by relevance, keeps the TOP, drops the tail
    history.py   ranks by recency,   keeps the END, drops the head

Retrieval can afford to discard the middle of a list, because the list was
sorted by how well each item answers the question. A conversation cannot: turn 4
is what makes turn 5 mean anything, and dropping turn 4 to keep turn 1 produces
a transcript that reads as if the user changed the subject. So the window slides
from the newest end backwards, and what falls off is the oldest.

The failure this exists to prevent
----------------------------------
**Unbounded context.** The obvious implementation appends every turn to every
prompt. Cost then grows linearly with thread length while answer quality does
not follow it — long contexts measurably bury the relevant part — so a
three-month thread costs fifty times a fresh one and answers worse. The budget
is enforced here, in tokens, counted with the same tokeniser everything else in
this codebase counts with.

What a recency window loses, and what compensates
-------------------------------------------------
It forgets. Something the user said forty turns ago is simply gone from the
prompt, and no amount of window tuning changes that — a bigger window is a more
expensive version of the same limit.

The mechanism that compensates is **long-term memory** (`app/memory/`), which is
why these two arrived in one milestone rather than two. Extraction lifts durable
facts out of a conversation *before* the window drops them, and recall puts them
back on demand. A bounded window is only survivable because something else
remembers; memory is only worth having because the window is bounded.

Summarising the dropped turns instead is the other well-known answer, and it is
deliberately not here. It costs a model call on the user's turn — which
`docs/agents.md` rule 5 forbids — and a summariser that quietly drops the one
clause that mattered fails invisibly. There is no API key in this environment to
measure whether ours would, and shipping an unmeasured summariser is how a
system starts confidently misremembering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.rag.chunking import count_tokens

SPEAKERS = {"user": "User", "assistant": "Assistant", "tool": "Tool"}
"""How each role is labelled to the model.

Capitalised English words rather than the stored enum values, because this is
prose the model reads, not data it parses. The stored value stays lowercase (see
`MessageRole`); the two never need to agree.
"""

TURN_TEMPLATE = "{speaker}: {content}"
TURN_SEPARATOR = "\n"
HISTORY_HEADER = "Earlier in this conversation:"

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class HistoryTurn:
    """One past turn, reduced to what a prompt needs.

    A plain pair rather than the `Message` ORM object, for the reason every
    boundary in this codebase gives: this is read inside a graph whose state is
    checkpointed as JSON, and an ORM object in state is a lazy load waiting to
    raise `MissingGreenlet` from somewhere unhelpful.
    """

    role: str
    content: str


@dataclass(frozen=True)
class SelectedHistory:
    """The turns that fit, rendered, plus what it cost.

    `dropped` is reported rather than swallowed — the same rule as
    `AssembledContext.dropped`. It is the only signal that a thread has outgrown
    its window, and the operator deciding whether to raise the budget is the one
    who needs to see it.
    """

    turns: list[HistoryTurn]
    text: str
    tokens: int
    dropped: int

    @property
    def is_empty(self) -> bool:
        return not self.turns


def select_history(turns: list[HistoryTurn], *, budget: int) -> SelectedHistory:
    """Keep the most recent turns that fit in `budget` tokens.

    Walks backwards from the newest turn and stops at the first one that does
    not fit — it does **not** skip a long turn to squeeze in an older short one,
    which is the one place this deliberately differs from `assemble_context`.

    There, skipping is right: results are independent, so dropping a long chunk
    to fit two short ones costs nothing but relevance. Here it would produce a
    transcript with a hole in the middle, and a hole in a conversation does not
    read as missing — it reads as a non-sequitur the model then tries to
    explain. Continuity is worth more than density.

    Whitespace inside a turn is collapsed to a single space. That is partly
    tidiness and mostly a real coupling: `app/llm/offline.py` finds context
    blocks by matching `[n]` at the start of a line, and an assistant reply
    containing a line that begins with a citation marker would otherwise be
    parsed as a source. One line per turn makes that unrepresentable, and
    `tests/unit/test_history.py` asserts it.
    """
    if budget <= 0:
        message = f"budget must be positive, got {budget}"
        raise ValueError(message)

    kept: list[HistoryTurn] = []
    used = count_tokens(HISTORY_HEADER)
    dropped = 0

    for turn in reversed(turns):
        rendered = _render(turn)
        cost = count_tokens(rendered) + count_tokens(TURN_SEPARATOR)

        if used + cost > budget:
            # Everything older than this is dropped too, without measuring each
            # one: once the window closes it stays closed, and counting the rest
            # would only produce a more precise number for the same outcome.
            dropped = len(turns) - len(kept)
            break

        kept.append(turn)
        used += cost

    kept.reverse()

    if not kept:
        return SelectedHistory(turns=[], text="", tokens=0, dropped=dropped)

    body = TURN_SEPARATOR.join(_render(turn) for turn in kept)
    text = f"{HISTORY_HEADER}\n{body}"

    # Measured at the end rather than trusted from the running total, because
    # token counts are not additive under concatenation — BPE merges across a
    # join. That cost this project a real bug in `chunking.py` at M6, and the
    # running total's only job is deciding what fits.
    return SelectedHistory(turns=kept, text=text, tokens=count_tokens(text), dropped=dropped)


def _render(turn: HistoryTurn) -> str:
    speaker = SPEAKERS.get(turn.role, turn.role.title())
    return TURN_TEMPLATE.format(speaker=speaker, content=_WHITESPACE.sub(" ", turn.content).strip())
