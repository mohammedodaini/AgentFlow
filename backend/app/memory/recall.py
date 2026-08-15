"""Finding the memories worth putting in front of the model.

Layer: memory. The read half; `writer.py` is the write half.

Recall is not retrieval, even though both start with a vector search
-------------------------------------------------------------------
Retrieval (M6) answers "which passages are most similar to this question?" and
that is the whole question, because a passage is either relevant or it is not.

Recall answers something messier: "which of the things we believe are worth
raising *now*?" Similarity is necessary and nowhere near sufficient. A fact
mentioned once, six months ago, may match a question perfectly and still be the
wrong thing to say — it is probably out of date, and unlike a document chunk
there is no citation for the user to check it against.

So the rank is a blend:

    score = similarity × (1 + IMPORTANCE_WEIGHT × importance) × recency

Multiplicative, not additive, and that choice is load-bearing. A sum lets a very
important memory surface for a question it has nothing to do with — importance
alone clears the bar. A product cannot: a similarity of zero is a score of zero
however important or fresh the memory is. Importance and recency *modulate*
relevance here; they never substitute for it.

The M8 finding applies, and it applies harder
----------------------------------------------
M8 measured that no non-zero similarity threshold usefully separates answerable
from unanswerable questions — the distributions overlap outright. The same limit
holds here, so recall applies only the degenerate floor (`MIN_RECALL_SCORE`),
exactly as `Generator` does: a memory with *literally zero* similarity is not
evidence of anything and is not sent.

The consequence deserves stating plainly, because it is a real gap and not a
detail: **nothing here can tell a relevant memory from a merely word-overlapping
one.** With the offline hashing embedder that is severe, since it matches shared
words rather than shared meaning. The blend narrows the damage by requiring
similarity *and* importance *and* recency together, which is a mitigation rather
than a fix.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.policies import recency_factor, reinforce
from app.rag.embeddings import EmbeddingProvider
from app.repositories.memory_repository import (
    CANDIDATE_MULTIPLIER,
    MAX_TOP_K,
    MemoryRepository,
    ScoredMemory,
)

logger = structlog.get_logger(__name__)

IMPORTANCE_WEIGHT = 0.5
"""How much importance may amplify a similarity score.

At 0.5 a maximally important memory ranks 1.5× a worthless one of equal
similarity — enough to reorder near-ties, not enough to promote something
irrelevant. The bound matters more than the value: any weight large enough to
let importance overturn a large similarity gap recreates the additive failure
this blend exists to avoid.
"""

MIN_RECALL_SCORE = 1e-9
"""A memory with no measurable similarity is not recalled.

The same constant, for the same reason, as `MIN_EVIDENCE_SCORE` in
`app/rag/generation.py` — and it is here because of how that one was found. A
vector search always returns its `limit` nearest neighbours however far away
they are, so "nothing relevant" is not a state pgvector can report. Without this
floor, a recall against a store of three unrelated memories would return all
three and present them to the model as context.
"""

DEFAULT_TOP_K = 3
"""Memories per turn. Small, because each one spends prompt budget that a cited
document chunk could have used, and an uncited assertion earns its place less
easily than a quotable passage does."""


class Recaller:
    """Retrieves memories relevant to the current turn, and reinforces them."""

    def __init__(self, session: AsyncSession, embedder: EmbeddingProvider) -> None:
        self._memories = MemoryRepository(session)
        self._embedder = embedder

    async def recall(
        self,
        organization_id: uuid.UUID,
        query: str,
        *,
        user_id: uuid.UUID | None = None,
        top_k: int = DEFAULT_TOP_K,
        touch: bool = True,
    ) -> list[ScoredMemory]:
        """The memories most worth raising for `query`, best first.

        `touch=False` exists for one kind of caller: anything that wants to *see*
        what recall would return without changing it. A debugging endpoint that
        reinforced every memory it displayed would make inspecting the store
        alter the store, and the numbers an operator was reading would be the
        ones their own reading produced.
        """
        if not query.strip():
            # Not embedded. An empty vector is not "no query" to pgvector; it is
            # a point in space, and it has nearest neighbours like any other.
            return []

        if not 0 < top_k <= MAX_TOP_K:
            message = f"top_k must be in (0, {MAX_TOP_K}], got {top_k}"
            raise ValueError(message)

        embedding = await self._embedder.embed_query(query)
        candidates = await self._memories.nearest(
            organization_id,
            embedding,
            user_id=user_id,
            # Over-fetch, because the blend cannot be an ORDER BY without
            # discarding the HNSW index. See the repository's module docstring.
            limit=top_k * CANDIDATE_MULTIPLIER,
        )

        now = datetime.now(UTC)
        scored: list[ScoredMemory] = []

        for memory, similarity in candidates:
            recency = recency_factor(memory.last_accessed_at, now)
            scored.append(
                ScoredMemory(
                    memory_id=memory.id,
                    content=memory.content,
                    scope=memory.scope,
                    similarity=similarity,
                    importance=memory.importance,
                    recency=recency,
                    score=similarity * (1.0 + IMPORTANCE_WEIGHT * memory.importance) * recency,
                )
            )

        recalled = sorted(
            (memory for memory in scored if memory.score > MIN_RECALL_SCORE),
            key=lambda memory: memory.score,
            reverse=True,
        )[:top_k]

        if touch and recalled:
            # Reinforce *what was returned*, not what was considered. A candidate
            # that lost the re-rank was never shown to the model, so counting it
            # as used would make every over-fetch quietly strengthen memories
            # nobody saw — and `CANDIDATE_MULTIPLIER` would become a popularity
            # knob rather than a performance one.
            await self._memories.touch(
                {memory.memory_id: reinforce(memory.importance) for memory in recalled}
            )

        logger.debug(
            "memory.recalled",
            organization_id=str(organization_id),
            candidates=len(candidates),
            returned=len(recalled),
            top_score=recalled[0].score if recalled else 0.0,
        )
        return recalled
