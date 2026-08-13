"""Text → chunks. A pure function, and the most consequential one in RAG.

Layer: rag. No I/O, no database, no network — which is why it can be tested
exhaustively and cheaply, and why it should be.

Chunking quality caps retrieval quality. A chunk that splits a sentence in half
cannot be retrieved by a question about either half; a chunk that swallows
three unrelated sections dilutes its own embedding until it matches nothing in
particular. Everything downstream — embeddings, ranking, the answer at M7 — is
bounded by the decisions made here.

The numbers (400 tokens, 60 overlap) are a *starting point*, not a finding.
`docs/roadmap.md` is explicit that they get tuned at M8 against a golden set,
"using eval metrics, not vibes". Until then they are vibes, and saying so is
more useful than defending them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

import tiktoken

ENCODING_NAME = "cl100k_base"
"""The tokeniser to count with.

Not the same family Claude uses, and that is acceptable: this number decides
chunk *geometry*, where being within ten percent is fine, and the alternative
is a network round trip per count. What it must never be is a character-count
approximation — the ratio of characters to tokens swings by a factor of three
between English prose and a table of numbers, so a chunk that fits comfortably
in a test would blow a context window in production.
"""

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n+")
"""A blank line. The strongest structural signal available in plain text
without parsing markup, and the boundary a human would pick."""

SEPARATOR = "\n\n"
"""What rejoins units inside a chunk — and it is not free.

The separator costs tokens too. Omitting it from the budget is how a chunker
that "never exceeds chunk_size" quietly exceeds it by one token per joined
paragraph; a test caught exactly that here. Anything that contributes
characters to the output has to be counted.
"""

MIN_CHUNK_SIZE = 8
"""Below this there is no room for a tail, a separator and any content at all,
and the geometry stops meaning anything."""


@dataclass(frozen=True)
class Chunk:
    """One slice of a document, ready to embed.

    Frozen because a chunk is a value, not a thing that changes: once its text
    and index are decided they are what gets embedded and stored, and anything
    wanting different text wants a different chunk.
    """

    index: int
    content: str
    token_count: int


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    """The tokeniser, loaded once per process.

    Cached because construction reads (and on a cold machine, downloads) a
    multi-megabyte BPE table — several seconds the first time, and unacceptable
    per chunk. `maxsize=1` because there is exactly one encoding in use; a
    larger cache would only hide the day a second one appears.
    """
    return tiktoken.get_encoding(ENCODING_NAME)


def count_tokens(text: str) -> int:
    """How many tokens `text` costs. The unit every budget here is denominated in."""
    return len(_encoding().encode(text))


def chunk_text(text: str, *, chunk_size: int, overlap: int) -> list[Chunk]:
    """Split `text` into overlapping, token-bounded chunks.

    Two rules, in priority order.

    **Never exceed `chunk_size` tokens.** A hard bound, not a target: the
    number exists because something downstream — an embedding model's input
    limit, a context window — will reject or silently truncate anything larger.

    **Break at paragraphs where possible.** Paragraphs are pre-existing
    semantic boundaries that cost nothing to respect. A chunk ending
    mid-sentence embeds as something neither half of that sentence means.

    Consecutive chunks share `overlap` tokens. Overlap is insurance against the
    one failure a fixed boundary guarantees: a fact stated across a boundary —
    "the deadline is" | "the 15th of March" — is otherwise in no chunk at all,
    and no query can retrieve it. The cost is duplicated storage and embedding
    spend on roughly `overlap / chunk_size` of the corpus.

    Returns an empty list for empty or whitespace-only input, rather than one
    empty chunk. An embedded empty string is a vector with no meaning that
    still competes in every search.
    """
    if chunk_size < MIN_CHUNK_SIZE:
        message = f"chunk_size must be at least {MIN_CHUNK_SIZE}, got {chunk_size}"
        raise ValueError(message)

    if not 0 <= overlap <= chunk_size // 2:
        # Capped at half, not merely below chunk_size. Equal would never
        # advance — each chunk would begin with exactly the tokens the previous
        # one ended with, and the function would allocate until the process
        # died — and anything above half produces chunks that are mostly copies
        # of their neighbour, which costs storage and embedding spend to make
        # retrieval worse. Half also guarantees room for content beside a tail.
        message = (
            f"overlap must be in [0, chunk_size // 2], got {overlap} with chunk_size {chunk_size}"
        )
        raise ValueError(message)

    if not text.strip():
        return []

    encoding = _encoding()
    separator_tokens = len(encoding.encode(SEPARATOR))

    # Units are pieces guaranteed to fit *alongside* an overlap tail and the
    # separator between them, which is what keeps the hard bound exact rather
    # than approximate: tail (≤ overlap) + separator + unit (≤ budget) can
    # never exceed chunk_size.
    budget = max(1, chunk_size - overlap - separator_tokens)
    units = _split_into_units(text, budget)

    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    carried_tail = ""

    for unit in units:
        unit_tokens = len(encoding.encode(unit))
        addition = unit_tokens + (separator_tokens if current else 0)

        if current and current_tokens + addition > chunk_size:
            body = SEPARATOR.join(current)
            chunks.append(body)

            carried_tail = _tail_tokens(body, overlap)
            current = [carried_tail] if carried_tail else []
            current_tokens = len(encoding.encode(carried_tail)) if carried_tail else 0
            # Recomputed: whether a separator is owed depends on whether the
            # reset left a tail behind.
            addition = unit_tokens + (separator_tokens if current else 0)

        current.append(unit)
        current_tokens += addition

    if current:
        body = SEPARATOR.join(current)
        # A final chunk consisting of nothing but the previous chunk's tail
        # carries no new text, and would be a duplicate competing against its
        # own source in every ranking.
        if body.strip() and body != carried_tail:
            chunks.append(body)

    return [
        Chunk(index=index, content=content, token_count=len(encoding.encode(content)))
        for index, content in enumerate(_enforce_limit(chunks, chunk_size))
    ]


def _enforce_limit(bodies: list[str], chunk_size: int) -> list[str]:
    """Guarantee the hard bound by measuring the finished text, not the parts.

    This pass exists because **token counts are not additive under
    concatenation**. Byte-pair encoding merges across a join, so
    `encode(a + sep + b)` can be *longer* than `encode(a) + encode(sep) +
    encode(b)`. The packing loop above budgets by summing pieces, which is fast
    and very nearly right — and "very nearly" is not what a hard bound means.

    A test at `chunk_size=8` produced a 9-token chunk with every piece
    individually within budget, which is exactly this effect. So the assembled
    body is measured once and split if it is still over. Cheap: one encode per
    chunk, and for realistic geometry the split branch never runs.
    """
    encoding = _encoding()
    enforced: list[str] = []

    for body in bodies:
        if len(encoding.encode(body)) <= chunk_size:
            enforced.append(body)
            continue

        remainder = body

        while remainder.strip():
            piece = _truncate_to(remainder, chunk_size)

            if not piece:
                break

            enforced.append(piece.strip())
            remainder = remainder[len(piece) :]

    return [body for body in enforced if body.strip()]


def _truncate_to(text: str, limit: int) -> str:
    """The longest prefix of `text` that encodes to at most `limit` tokens.

    The trailing loop is not paranoia: decoding a token slice can cut a
    multi-byte character in half, and re-encoding the replacement character
    that results may cost *more* tokens than the slice did. Trimming until it
    genuinely fits is the only way to be certain.
    """
    encoding = _encoding()
    tokens = encoding.encode(text)

    if len(tokens) <= limit:
        return text

    candidate = encoding.decode(tokens[:limit])

    while candidate and len(encoding.encode(candidate)) > limit:
        candidate = candidate[:-1]

    return candidate


def _split_into_units(text: str, budget: int) -> list[str]:
    """Break text into paragraph-sized pieces, none larger than `budget` tokens.

    Paragraphs come first because they are free semantic boundaries. A
    paragraph that is itself too large — a wall-of-text export, a minified
    table — is cut on token boundaries instead, which is ugly and is the
    correct fallback: the hard bound is not negotiable, and the alternative is
    a chunk the embedding API rejects.
    """
    encoding = _encoding()
    units: list[str] = []

    for paragraph in _PARAGRAPH_BREAK.split(text):
        stripped = paragraph.strip()

        if not stripped:
            continue

        tokens = encoding.encode(stripped)

        if len(tokens) <= budget:
            units.append(stripped)
            continue

        for start in range(0, len(tokens), budget):
            piece = encoding.decode(tokens[start : start + budget]).strip()
            if piece:
                units.append(piece)

    return units


def _tail_tokens(text: str, count: int) -> str:
    """The last `count` tokens of `text`, as text.

    Decoding a token slice can cut a multi-byte character in half, and tiktoken
    substitutes a replacement character when it does. Acceptable here — the
    tail is redundant context, and one mangled character at the head of a chunk
    shifts its embedding far less than losing the sentence spanning the
    boundary would.
    """
    if count <= 0:
        return ""

    encoding = _encoding()
    tail = encoding.decode(encoding.encode(text)[-count:])

    # Decoding a token slice and re-encoding it is not guaranteed to be the
    # identity: a slice can start mid-character, and the re-encode may split
    # differently and come back *longer* than `count`. Left unchecked that
    # silently inflates the next chunk past the hard bound, so the tail is
    # trimmed until it genuinely fits.
    while tail and len(encoding.encode(tail)) > count:
        tail = tail[1:]

    return tail
