"""/search — retrieval alone, with no model in the loop (M6).

Layer: api. `/ask` joins this module at M7 and adds generation on top.

Shipping search *before* generation is deliberate sequencing, not an accident
of the roadmap. Retrieval quality caps answer quality absolutely: a model given
the wrong three paragraphs writes a fluent, confident, wrong answer, and the
failure looks like a model problem. This endpoint makes retrieval inspectable
on its own — a human can read the chunks and the scores and see immediately
whether the right passage was found — which is also exactly the surface M8's
metrics measure.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentMembership
from app.db.deps import get_db
from app.rag.embeddings import EmbeddingProvider, get_embedder
from app.rag.retrieval import Retriever
from app.schemas.document import SearchRequest, SearchResult

router = APIRouter(tags=["retrieval"])

SessionDep = Annotated[AsyncSession, Depends(get_db)]
EmbedderDep = Annotated[EmbeddingProvider, Depends(get_embedder)]


@router.post("/search", summary="Find the passages most relevant to a question")
async def search(
    request: SearchRequest,
    membership: CurrentMembership,
    session: SessionDep,
    embedder: EmbedderDep,
) -> list[SearchResult]:
    """Rank this organization's chunks against a query.

    `CurrentMembership`, so the search is scoped to one tenant and the caller
    has proved membership of it. That scoping is enforced *again* in SQL, by a
    join in `ChunkRepository.similarity_search` — belt and braces, because the
    consequence of getting it wrong here is returning another customer's
    documents, ranked helpfully by relevance.

    Returns a bare list rather than a `Page`. Search results are a ranked
    top-`k`, not a paginated collection: there is no meaningful "total" —
    every chunk in the corpus has *some* similarity — and offering `offset`
    would invite clients to page through a ranking whose tail is noise by
    construction.
    """
    results = await Retriever(session, embedder).retrieve(
        membership.organization_id, request.query, top_k=request.top_k
    )

    return [
        SearchResult(
            chunk_id=result.chunk_id,
            document_id=result.document_id,
            document_title=result.document_title,
            chunk_index=result.chunk_index,
            content=result.content,
            score=result.score,
        )
        for result in results
    ]
