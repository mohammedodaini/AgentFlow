"""Query → ranked chunks with citations. The heart of RAG.

Layer: rag. Deliberately thin: embed the query, ask the repository for
neighbours, apply a relevance floor. The complexity lives in
`ChunkRepository.similarity_search`, where the SQL is, and this class exists so
`/search`, `/ask` (M7) and the RAG agent's `search_chunks` tool (M9) each make
one call rather than keeping three copies of the same three steps.

What is deliberately *not* here yet: reranking. A cross-encoder pass over the
top 50 candidates is the standard next improvement and often a large one — and
adding it now would be spending latency and money on the strength of a blog
post. M8 builds the golden set that can say whether it helps *this* corpus. The
roadmap is explicit about the order: measure, then optimise.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.embeddings import EmbeddingProvider
from app.repositories.chunk_repository import ChunkRepository, ScoredChunk

logger = structlog.get_logger(__name__)

DEFAULT_MIN_SCORE = 0.0
"""No relevance floor. **Measured at M8, not guessed.**

M6 left this at zero on the argument that picking a threshold by eye means
silently returning nothing for questions whose answer sits just under an
arbitrary line. M8 built the instrument to check that, and the answer came back
stronger than the argument: on the `handbook` golden set, *no* non-zero
threshold beats zero.

    threshold   correct refusals   answerable lost   overall accuracy
        0.000              0 / 4            0 / 11              0.733
        0.100              1 / 4            2 / 11              0.667
        0.220              2 / 4            6 / 11              0.467
        0.263              4 / 4            9 / 11              0.400

The two populations overlap outright — the lowest-scoring *answerable* question
scores 0.069 while the highest-scoring *unanswerable* one scores 0.262 — so
every threshold that catches a refusal discards more real answers than it saves.

The conclusion is the useful part: **refusal is a judgement about meaning, and a
similarity score cannot make it.** It belongs to the model reading the context,
which is what `app/prompts/rag/system.md` instructs and what
`MIN_EVIDENCE_SCORE` in `generation.py` deliberately does not attempt.

Caveat, stated plainly: measured with the offline hashing embedder, which
matches words rather than meaning. A semantic embedder should separate these
populations far better, and the measurement must be repeated once a key exists.
What generalises here is the method, not the number.
"""


class Retriever:
    """Finds the chunks most likely to answer a question.

    Takes the session and the embedder rather than building them: the session
    belongs to the request, and the embedder is built once per process because
    it owns an HTTP client. Constructing either here would open a connection
    pool per search.
    """

    def __init__(self, session: AsyncSession, embedder: EmbeddingProvider) -> None:
        self._chunks = ChunkRepository(session)
        self._embedder = embedder

    async def retrieve(
        self,
        organization_id: uuid.UUID,
        query: str,
        *,
        top_k: int = 5,
        min_score: float = DEFAULT_MIN_SCORE,
    ) -> list[ScoredChunk]:
        """Rank this organization's chunks against `query`.

        The query is embedded with `embed_query`, not `embed_documents`. They
        are the same call for both current providers and they are not the same
        *contract* — asymmetric models expect different prefixes on questions
        and passages, and using the wrong one degrades ranking without failing.

        An empty or whitespace query returns nothing rather than embedding it.
        A vector built from no words matches arbitrarily, so the alternative is
        five confident, unrelated results for a query the user never made.
        """
        if not query.strip():
            return []

        embedding = await self._embedder.embed_query(query)
        results = await self._chunks.similarity_search(organization_id, embedding, top_k=top_k)

        if min_score > 0.0:
            results = [result for result in results if result.score >= min_score]

        logger.info(
            "retrieval.retrieved",
            organization_id=str(organization_id),
            characters=len(query),
            returned=len(results),
            top_score=results[0].score if results else None,
        )
        return results
