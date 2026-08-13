"""/ask — retrieval with a model on top (M7).

Layer: api. `/search` (M6) returns the passages; this returns the answer. Both
survive, because they answer different questions and the cheap one is the only
way to debug the expensive one: when an answer is wrong, the first thing anyone
needs to know is whether the right passage was ever retrieved.

Two endpoints, one operation
----------------------------
`POST /ask` returns the whole answer as JSON. `POST /ask/stream` returns the
same thing over Server-Sent Events. The duplication is deliberate: streaming is
strictly harder to consume — no status code after the first byte, no
`response.json()`, manual reassembly — and a service-to-service caller or a test
should not pay that cost for a latency benefit only a human perceives.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentMembership
from app.core.config import Settings, get_settings
from app.db.deps import get_db
from app.llm import get_llm
from app.llm.base import LLMError, LLMProvider
from app.rag.context import Source
from app.rag.embeddings import EmbeddingProvider, get_embedder
from app.rag.generation import Generator
from app.schemas.ask import AskRequest, AskResponse, AskUsage, Citation

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["generation"])

SessionDep = Annotated[AsyncSession, Depends(get_db)]
EmbedderDep = Annotated[EmbeddingProvider, Depends(get_embedder)]
LLMDep = Annotated[LLMProvider, Depends(get_llm)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

SSE_MEDIA_TYPE = "text/event-stream"


@router.post("/ask", summary="Answer a question from this organization's documents")
async def ask(
    request: AskRequest,
    membership: CurrentMembership,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> AskResponse:
    """Retrieve, ground, generate, and cite.

    `CurrentMembership`, so the corpus searched belongs to one tenant and the
    caller has proved membership of it — enforced again in SQL by the join in
    `ChunkRepository.similarity_search`. The stakes are higher here than at
    `/search`: a leak there returns another customer's passages, while a leak
    here *summarises them in fluent prose*, which is both worse and far harder
    to notice.

    Returns 200 with an explicit refusal when nothing is retrieved, rather than
    404. Nothing is missing — the question was answered, and the answer is that
    the documents do not cover it.
    """
    answer = await Generator(session, embedder, llm, settings).answer(
        membership.organization_id, request.query, top_k=request.top_k
    )

    return AskResponse(
        answer=answer.text,
        citations=[_citation(source) for source in answer.sources],
        model=answer.model,
        usage=AskUsage(
            input_tokens=answer.input_tokens,
            output_tokens=answer.output_tokens,
            context_tokens=answer.context_tokens,
            dropped_sources=answer.dropped_sources,
        ),
        truncated=answer.truncated,
    )


@router.post(
    "/ask/stream",
    summary="Answer a question, streaming the text as it is generated",
    response_class=StreamingResponse,
    responses={200: {"content": {SSE_MEDIA_TYPE: {}}, "description": "SSE event stream"}},
)
async def ask_stream(
    request: AskRequest,
    membership: CurrentMembership,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> StreamingResponse:
    """The same answer, as Server-Sent Events.

    SSE rather than WebSockets. The traffic is one-directional and short-lived,
    which is exactly what SSE is for; it rides on plain HTTP, so proxies,
    authentication and error handling all work unchanged; and the browser API
    reconnects on its own. A WebSocket would buy bidirectionality nothing here
    needs, in exchange for a second transport to secure and operate.

    Sources are sent **first**, in their own event, because retrieval completes
    before the first token exists. A client can render its citations
    immediately — the difference between an answer that arrives with its
    evidence and one whose evidence appears after the reader has finished.
    """
    sources, tokens = await Generator(session, embedder, llm, settings).stream_answer(
        membership.organization_id, request.query, top_k=request.top_k
    )

    return StreamingResponse(
        _events(sources, tokens),
        media_type=SSE_MEDIA_TYPE,
        headers={
            # Buffering proxies are the classic way a streaming endpoint tests
            # perfectly and then delivers nothing until the response completes.
            # nginx honours `X-Accel-Buffering`; the others stop intermediaries
            # caching what is always a per-tenant answer.
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


async def _events(sources: list[Source], tokens: AsyncIterator[str]) -> AsyncIterator[str]:
    """Frame the answer as SSE.

    Named events rather than one anonymous stream, so a client dispatches on
    `event:` instead of guessing from each payload's shape.

    The `error` event exists because of the one thing streaming cannot do: once
    the first byte is written the status code has already been sent, so a
    failure mid-answer *cannot* become a 502. Without an explicit error event, a
    connection that dies halfway is indistinguishable from one that finished —
    and the client renders a truncated answer as a complete one.
    """
    yield _event("sources", [_citation(source).model_dump(mode="json") for source in sources])

    try:
        async for text in tokens:
            yield _event("token", {"text": text})
    except LLMError as error:
        logger.warning("generation.stream_failed", error=error.message)
        yield _event("error", {"code": error.code, "message": error.message})
        return

    yield _event("done", {})


def _event(name: str, data: object) -> str:
    """One SSE frame.

    The trailing blank line is the frame terminator — without it the client
    buffers forever, having been sent a message that never ends. `separators`
    drops the spaces `json.dumps` adds by default: a few bytes per frame, on a
    protocol that sends one frame per token.
    """
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _citation(source: Source) -> Citation:
    return Citation(
        number=source.number,
        chunk_id=source.chunk_id,
        document_id=source.document_id,
        document_title=source.document_title,
        chunk_index=source.chunk_index,
        score=source.score,
    )
