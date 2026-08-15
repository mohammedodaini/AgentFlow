"""arq task: `extract_memories` — learning from a conversation, afterwards.

Layer: workers. `docs/agents.md` rule 5: memory is asynchronous, because
extraction is a second model call whose result nobody is waiting for, and putting
it on the request path would roughly double the latency of every message to
produce something the user will not see until their *next* one.

Idempotent, like every task here, and by a different mechanism
--------------------------------------------------------------
`ingest_document` (M5) achieves idempotence by *replacing* a document's chunks.
This one cannot replace anything — it is additive by nature — so it relies on the
uniqueness constraint over `(organization_id, scope, user_id, content_hash)` and
on `MemoryWriter`'s similarity check. Running twice over the same exchange
produces the same facts, finds them already present, and stores nothing.

Worth stating, because the naive version of this task doubles every memory on an
arq retry — and duplicate memories are worse than duplicate chunks: they crowd a
much smaller budget, and each one is an uncited assertion.

Honest about which failures are worth retrying
-----------------------------------------------
A model that ignored the output format will ignore it again, so `MemoryWriter`
returns rather than raising and the job ends successfully having stored nothing.
Infrastructure failures — Postgres restarting, Redis blipping — propagate, and
arq retries them. The same split as `ingest_document`, for the same reason.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.history import HistoryTurn
from app.llm.base import LLMProvider
from app.memory.writer import MemoryWriter
from app.models.task import Task, TaskStatus
from app.rag.embeddings import EmbeddingProvider
from app.repositories.conversation_repository import MAX_HISTORY_MESSAGES, ConversationRepository

logger = structlog.get_logger(__name__)


async def extract_memories(
    ctx: dict[str, Any],
    *,
    conversation_id: str,
    organization_id: str,
    user_id: str | None,
    task_id: str,
    agent_run_id: str | None,
) -> dict[str, Any]:
    """Read a finished exchange and store what is worth remembering.

    Ids arrive as strings because the queue payload is serialised; they are
    parsed back here rather than trusted, so a malformed job fails loudly at the
    boundary instead of deep inside a query.
    """
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    embedder: EmbeddingProvider = ctx["embedder"]
    llm: LLMProvider = ctx["llm"]

    conversation_uuid = uuid.UUID(conversation_id)
    organization_uuid = uuid.UUID(organization_id)
    task_uuid = uuid.UUID(task_id)
    user_uuid = uuid.UUID(user_id) if user_id else None
    run_uuid = uuid.UUID(agent_run_id) if agent_run_id else None

    log = logger.bind(
        conversation_id=conversation_id, task_id=task_id, job_try=ctx.get("job_try", 1)
    )

    async with session_factory() as session:
        task = await _start_task(session, task_uuid)
        conversations = ConversationRepository(session)

        # Tenant-scoped, in a worker with no request and no user watching. The
        # worker is not exempt from tenancy just because nobody is looking: an
        # unscoped `SELECT ... WHERE id = ?` here is the same bug as in a route,
        # discovered later and by somebody else.
        conversation = await conversations.get(organization_uuid, conversation_uuid)

        if conversation is None:
            # The thread was deleted between the turn and this job. Not an error:
            # there is nothing to learn from, and nothing to fix.
            log.info("memory.conversation_gone")
            await _finish_task(session, task, {"stored": 0, "reason": "conversation_gone"})
            await session.commit()
            return {"stored": 0}

        turns = [
            HistoryTurn(role=message.role.value, content=message.content)
            for message in await conversations.recent_messages(
                conversation_uuid, limit=MAX_HISTORY_MESSAGES
            )
        ]

        result = await MemoryWriter(session, embedder, llm).extract_and_store(
            organization_uuid, user_uuid, turns, source_run_id=run_uuid
        )

        await _finish_task(
            session,
            task,
            {
                "stored": result.stored,
                "duplicates": result.duplicates,
                "reinforced": result.reinforced,
            },
        )
        await session.commit()

    log.info("memory.extraction_complete", stored=result.stored, duplicates=result.duplicates)
    return {"stored": result.stored, "duplicates": result.duplicates}


async def _start_task(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    """Mark the durable mirror of this job as running, and count the attempt.

    Returns None when the row is missing rather than raising. The `tasks` row is
    a record for humans, not a precondition for the work — failing an extraction
    because its bookkeeping row was pruned would trade something valuable for
    something that is not.
    """
    task = await session.get(Task, task_id)

    if task is None:
        return None

    task.status = TaskStatus.RUNNING
    task.attempts += 1
    await session.flush()
    return task


async def _finish_task(session: AsyncSession, task: Task | None, result: dict[str, Any]) -> None:
    if task is None:
        return

    task.status = TaskStatus.SUCCEEDED
    task.result = result
    await session.flush()
