"""Chunk writes and vector search — the only place that writes pgvector SQL.

Layer: repositories. Takes a session, owns no transaction.

A repository is justified here more clearly than anywhere else in the codebase.
Similarity search is not "a query with a where clause": it joins for tenancy,
orders by an operator most people have never seen, depends on an index whose
operator class must match that operator exactly, and needs a session-level
setting to return correct results at all. Every one of those is a way to be
silently wrong, and all of them live in one method.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.document_chunk import DocumentChunk

logger = structlog.get_logger(__name__)

MAX_TOP_K = 50
"""Ceiling on results per search. Every returned chunk is context someone pays
for at M7, and precision at the top of the list matters far more than depth."""


@dataclass(frozen=True)
class ScoredChunk:
    """A retrieved chunk, its relevance, and what to cite for it.

    Carries the document's id and title rather than the `Document` object.
    Retrieval results are read-only by nature, and handing back an ORM object
    invites a lazy load in a route — which under asyncio surfaces as
    `MissingGreenlet` from inside serialisation, mentioning none of your code.
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    chunk_index: int
    content: str
    token_count: int
    score: float
    """Cosine *similarity* in [0, 1], where 1 is identical.

    Converted from the cosine *distance* pgvector returns, because every
    consumer — an API response, a relevance threshold, a human reading a log —
    finds "higher is better" the obvious reading, and a raw distance invites
    exactly one off-by-inversion bug that ranks the worst matches first.
    """


class ChunkRepository:
    """Reads and writes for `document_chunks`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_for_document(
        self, document_id: uuid.UUID, chunks: list[DocumentChunk]
    ) -> int:
        """Delete this document's chunks and insert the given ones.

        Replace rather than append, and in that order, because this runs in a
        task that may be delivered twice. Appending would double every chunk on
        a retry, and duplicate chunks do not announce themselves — they quietly
        take two of the top five slots in every search that matches them,
        crowding out results that would have been more useful.

        Both statements are in the caller's transaction, so a failure between
        them leaves the old chunks intact rather than none at all.
        """
        await self.delete_for_document(document_id)

        if not chunks:
            return 0

        self._session.add_all(chunks)
        await self._session.flush()
        return len(chunks)

    async def delete_for_document(self, document_id: uuid.UUID) -> int:
        """Remove every chunk of one document.

        A bulk `DELETE` rather than loading and deleting objects: a 300-page
        PDF has hundreds of chunks, and the ORM path would fetch every one of
        them — including its 1536-float embedding — purely to throw it away.
        """
        # `execute` is typed as returning `Result`, which has no `rowcount`; a
        # DML statement really returns a `CursorResult`, which does. The cast
        # says so rather than reaching for `getattr`, which would also silently
        # survive the day this stops being a DELETE.
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
            ),
        )
        await self._session.flush()
        return result.rowcount or 0

    async def count_for_document(self, document_id: uuid.UUID) -> int:
        total = await self._session.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
        )
        return total or 0

    async def similarity_search(
        self, organization_id: uuid.UUID, embedding: list[float], *, top_k: int = 5
    ) -> list[ScoredChunk]:
        """The nearest `top_k` chunks in this organization, closest first.

        **Tenancy is a join, not a filter on this table.** `document_chunks`
        has no `organization_id`; it reaches one through `documents`. Forgetting
        this join would not raise — it would return other customers' documents,
        ranked by relevance, which is the worst failure this system can have.

        **Cosine distance, matching the index.** `<=>` is cosine distance and
        the HNSW index is built with `vector_cosine_ops`. If those two ever
        disagree the query still returns rows — Postgres simply ignores the
        index and scans the table, so the only symptom is that search gets
        slower as the corpus grows.

        **`hnsw.iterative_scan` is why this stays correct under a filter.** An
        HNSW scan finds candidates by vector distance *first*, and the tenancy
        join filters them *after*. Ask for 5 chunks in a database where the
        nearest 40 belong to other organizations and a plain index scan returns
        fewer than 5 — possibly none — with no error at all. Iterative scan
        makes pgvector keep searching until it has enough surviving rows.
        `strict_order` rather than `relaxed_order` because these results are
        ranked citations: approximately ordered is fine for a feed and wrong
        for "here is the passage your answer came from".
        """
        if not 0 < top_k <= MAX_TOP_K:
            message = f"top_k must be in (0, {MAX_TOP_K}], got {top_k}"
            raise ValueError(message)

        # SET LOCAL, so it lasts exactly as long as this transaction and cannot
        # leak into whatever else reuses this pooled connection afterwards.
        await self._session.execute(text("SET LOCAL hnsw.iterative_scan = 'strict_order'"))

        distance = DocumentChunk.embedding.cosine_distance(embedding).label("distance")

        rows = await self._session.execute(
            select(DocumentChunk, Document.title, distance)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(Document.organization_id == organization_id)
            .order_by(distance)
            .limit(top_k)
        )

        results = [
            ScoredChunk(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                document_title=title,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                token_count=chunk.token_count,
                # Cosine distance is in [0, 2]; for the normalised vectors both
                # providers produce it stays in [0, 1], so this maps cleanly
                # onto a similarity. Clamped because floating-point error can
                # put it a hair outside, and a score of 1.0000000002 reads as
                # a bug to anyone looking at a response.
                score=max(0.0, min(1.0, 1.0 - float(chunk_distance))),
            )
            for chunk, title, chunk_distance in rows.all()
        ]

        logger.debug(
            "retrieval.searched",
            organization_id=str(organization_id),
            top_k=top_k,
            found=len(results),
        )
        return results
