"""Ranked chunks → a bounded prompt context, with a citation map.

Layer: rag. A pure function, like `chunking.py`, and for the same reason: no
I/O means it can be tested exhaustively and cheaply. Two expensive mistakes live
here.

**Spending without a bound.** Retrieval returns `top_k` chunks and the naive
step is to concatenate them. That works until a `top_k` of 50 meets a corpus of
long chunks, and then every question silently costs ten times what it did — no
error, just a bill. The budget is enforced here, in tokens, counted with the
same tokeniser the chunker uses.

**Losing the thread between an answer and its evidence.** The model is shown
`[1]`, `[2]`, `[3]`; the API returns citations the client renders. If those
numbers do not correspond to the same chunks, every citation in the product is
wrong and nothing raises. So the numbering and the citation list are built here,
together, in one loop — rather than assigned in one place and reconstructed in
another.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.rag.chunking import count_tokens
from app.repositories.chunk_repository import ScoredChunk

SOURCE_TEMPLATE = "[{number}] {title}\n{content}"
"""How one chunk appears to the model.

The number leads, because the model has to reproduce it in a citation and
anything it must copy should be the easiest thing on the line to find. The title
follows because provenance changes how a passage should be read —
"handbook.pdf" and "draft-proposal-v3.pdf" deserve different confidence, and
only the document name carries that.

`app/llm/offline.py` parses this format. The coupling is deliberate and tested;
see its module docstring.
"""

SOURCE_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class Source:
    """One numbered source, and everything needed to cite it.

    `number` is the model-facing label — 1-based, because `[0]` reads as a
    footnote error to every human who has ever seen a citation.
    """

    number: int
    chunk_id: str
    document_id: str
    document_title: str
    chunk_index: int
    score: float


@dataclass(frozen=True)
class AssembledContext:
    """The prompt block, its sources, and what it cost.

    `dropped` is reported rather than swallowed. A question answered from three
    of eight retrieved chunks is a different event from one answered from all
    eight — it is the signal that the budget is binding, and the operator
    deciding whether to raise it is the one who needs to see it.
    """

    text: str
    sources: list[Source]
    tokens: int
    dropped: int

    @property
    def is_empty(self) -> bool:
        return not self.sources


def assemble_context(results: list[ScoredChunk], *, budget: int) -> AssembledContext:
    """Fit the highest-ranked chunks into `budget` tokens.

    Takes results in the order retrieval produced them — most relevant first —
    and keeps them in it. Two consequences worth being explicit about.

    **Truncation drops the weakest, never the strongest.** Filling greedily from
    the top means the budget cuts the tail, which is the half most likely to be
    noise. A version that filled from the end, or that packed to maximise how
    many chunks fit, would drop the best match to make room for two marginal
    ones.

    **Relevance order is also presentation order.** Models weight earlier
    context more heavily, so the ranking does double duty: it decides what
    survives *and* what the model reads first.

    A chunk that does not fit is skipped, not truncated. Half a passage can
    change what it appears to say — a cut before "unless the invoice exceeds
    £500" inverts the rule it belonged to — and a citation pointing at a
    sentence the model never saw the end of is worse than no citation at all.
    """
    if budget <= 0:
        message = f"budget must be positive, got {budget}"
        raise ValueError(message)

    blocks: list[str] = []
    sources: list[Source] = []
    used = 0
    dropped = 0

    for result in results:
        number = len(sources) + 1
        block = SOURCE_TEMPLATE.format(
            number=number, title=result.document_title, content=result.content
        )

        # The separator is counted, because it is text the model is charged for.
        # Omitting it is the mistake the chunker made at M6 — a budget that is
        # correct right up until something joins two pieces together.
        cost = count_tokens(block) + (count_tokens(SOURCE_SEPARATOR) if blocks else 0)

        if used + cost > budget:
            # No `break`. A later chunk may be short enough to fit where this one
            # was not, and stopping here would discard evidence the budget could
            # afford. Numbering stays contiguous because it is derived from
            # `len(sources)` rather than from the loop index.
            dropped += 1
            continue

        blocks.append(block)
        used += cost
        sources.append(
            Source(
                number=number,
                chunk_id=str(result.chunk_id),
                document_id=str(result.document_id),
                document_title=result.document_title,
                chunk_index=result.chunk_index,
                score=result.score,
            )
        )

    text = SOURCE_SEPARATOR.join(blocks)

    # Measured once at the end rather than trusted from the running total. Token
    # counts are not additive under concatenation — BPE merges across a join —
    # which cost this project a real bug in `chunking.py` at M6. The running
    # total decides what fits; this number is what actually got sent.
    return AssembledContext(
        text=text,
        sources=sources,
        tokens=count_tokens(text) if text else 0,
        dropped=dropped,
    )
