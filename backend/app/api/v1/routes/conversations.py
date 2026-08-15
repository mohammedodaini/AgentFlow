"""/conversations — chat threads and their messages (M10).

Layer: api. Routes call `ConversationService`, never a graph and never a
repository.

Why sending a message is 200 and not 202
----------------------------------------
Upload (M5) answers 202 because ingestion is genuinely background work. A
conversation turn runs inline and answers 200 with the reply, for the reason
`POST /agent-runs` does: a turn takes seconds, and a poll loop for a two-second
wait is friction with no benefit.

The *memory extraction* a turn triggers is 202-shaped work, and it is handled the
way `documents.py` handles ingestion — committed as a `tasks` row, enqueued after
the commit, never waited on. `docs/agents.md` rule 5: memory must not add latency
to a user's turn.

Why the commit is explicit here
-------------------------------
`get_db` commits at the end of a successful request, which would normally make an
explicit commit redundant. It is not, and ADR-0008 is why: the job must not reach
Redis until the rows it reads are durable. The worker loads the conversation
*from Postgres*, so a job delivered a millisecond early finds the thread without
the exchange it exists to learn from — and succeeds, having extracted nothing,
with no error anywhere to explain it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentMembership
from app.core.config import Settings, get_settings
from app.db.deps import get_db
from app.llm import get_llm
from app.llm.base import LLMProvider
from app.rag.embeddings import EmbeddingProvider, get_embedder
from app.schemas.common import Page
from app.schemas.conversation import (
    ConversationCreate,
    ConversationRead,
    MessageCreate,
    MessageRead,
    TurnRead,
)
from app.services.conversation_service import ConversationService
from app.workers.queue import JobQueue, enqueue_memory_extraction, get_queue

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])

SessionDep = Annotated[AsyncSession, Depends(get_db)]
EmbedderDep = Annotated[EmbeddingProvider, Depends(get_embedder)]
LLMDep = Annotated[LLMProvider, Depends(get_llm)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
QueueDep = Annotated[JobQueue, Depends(get_queue)]


def _service(
    session: AsyncSession, embedder: EmbeddingProvider, llm: LLMProvider, settings: Settings
) -> ConversationService:
    return ConversationService(session, embedder, llm, settings)


@router.post("", summary="Start a conversation")
async def create_conversation(
    request: ConversationCreate,
    membership: CurrentMembership,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> ConversationRead:
    conversation = await _service(session, embedder, llm, settings).create(
        membership.organization_id, membership.user_id, title=request.title
    )
    return ConversationRead.model_validate(conversation)


@router.get("", summary="List your conversations")
async def list_conversations(
    membership: CurrentMembership,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    settings: SettingsDep,
    include_archived: Annotated[bool, Query(description="Include archived threads")] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ConversationRead]:
    """Yours, not the organization's.

    Scoped to the authenticated user in the repository. A chat thread is the most
    personal thing in this schema, and an org-wide view is a separate feature with
    its own permission check rather than the absence of a predicate.
    """
    conversations, total = await _service(session, embedder, llm, settings).list_for_user(
        membership.organization_id,
        membership.user_id,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )

    return Page[ConversationRead](
        items=[ConversationRead.model_validate(conversation) for conversation in conversations],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{conversation_id}/messages", summary="Read a transcript")
async def list_messages(
    conversation_id: uuid.UUID,
    membership: CurrentMembership,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[MessageRead]:
    """The most recent `limit` turns, oldest first.

    A bare list rather than a `Page`, deliberately, and the one place in this API
    that departs from the envelope rule. A transcript is not a result set: it is
    read from the end, rendered in order, and paged backwards through time — so
    `total`/`offset` describe the wrong axis, and offering them would invite
    clients to build the wrong pagination.
    """
    messages = await _service(session, embedder, llm, settings).messages(
        membership.organization_id, conversation_id, limit=limit
    )
    return [MessageRead.model_validate(message) for message in messages]


@router.post("/{conversation_id}/messages", summary="Send a message and get a reply")
async def send_message(
    conversation_id: uuid.UUID,
    request: MessageCreate,
    membership: CurrentMembership,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    settings: SettingsDep,
    queue: QueueDep,
) -> TurnRead:
    """Append the question, run the agent with the thread's history, reply."""
    turn = await _service(session, embedder, llm, settings).send(
        membership.organization_id, membership.user_id, conversation_id, request.content
    )

    # Durable first, advertised second. See the module docstring.
    await session.commit()

    try:
        await enqueue_memory_extraction(
            queue,
            conversation_id=conversation_id,
            organization_id=membership.organization_id,
            user_id=membership.user_id,
            task_id=turn.task.id,
            agent_run_id=turn.run.id,
        )
    except (OSError, RedisError):
        # Redis is down, and the exchange is already committed. Failing the
        # request would be a lie in the more damaging direction: the user has an
        # answer, and a client that retried would ask the question twice and pay
        # for it twice.
        #
        # Losing the extraction is genuinely cheap — the conversation is still in
        # Postgres, the `tasks` row still says `queued`, and a sweeper can
        # re-enqueue from it. That row is why the table exists rather than
        # trusting Redis to remember (app/models/task.py).
        logger.exception(
            "conversations.enqueue_failed",
            conversation_id=str(conversation_id),
            task_id=str(turn.task.id),
        )

    return TurnRead(
        conversation=ConversationRead.model_validate(turn.conversation),
        user_message=MessageRead.model_validate(turn.user_message),
        assistant_message=MessageRead.model_validate(turn.assistant_message),
    )
