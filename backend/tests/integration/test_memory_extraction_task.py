"""The extraction task, run the way arq runs it (M10).

`test_memory.py` covers what `MemoryWriter` decides. This covers the job around
it: reading the conversation back out of Postgres, honouring tenancy with nobody
watching, updating the `tasks` row, and surviving a thread deleted between the
turn and the job.

The idempotence test is the one that matters. `ingest_document` (M5) is
idempotent because it *replaces* a document's chunks; this task is additive by
nature and cannot replace anything, so it leans entirely on the unique
constraint. The naive version doubles every memory on an arq retry — and
duplicate memories are worse than duplicate chunks, because they crowd a much
smaller budget and each one is an uncited assertion.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import MessageRole
from app.models.task import EXTRACT_MEMORIES, Task, TaskStatus
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.memory_repository import MemoryRepository
from app.storage import ObjectStorage
from app.workers.tasks.memory_extraction import extract_memories
from tests.factories import make_org_with_owner
from tests.integration.test_memory import StubLLM
from tests.worker_harness import worker_context

FACTS = "- Works in the Berlin office\n- Approves invoices for their team"


async def seed_conversation(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[uuid.UUID, Task]:
    """A thread with one exchange in it, plus the task row the route would write."""
    conversations = ConversationRepository(session)
    conversation = await conversations.create(
        organization_id=organization_id, user_id=user_id, title="Expenses"
    )
    await conversations.add_message(
        conversation_id=conversation.id,
        role=MessageRole.USER,
        content="I work in the Berlin office and I approve invoices for my team.",
    )
    await conversations.add_message(
        conversation_id=conversation.id,
        role=MessageRole.ASSISTANT,
        content="Expenses are reimbursed monthly. [1]",
    )

    task = Task(
        organization_id=organization_id,
        kind=EXTRACT_MEMORIES,
        payload={"conversation_id": str(conversation.id)},
        status=TaskStatus.QUEUED,
    )
    session.add(task)
    await session.flush()
    return conversation.id, task


async def run_task(
    session: AsyncSession,
    storage: ObjectStorage,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None,
    task_id: uuid.UUID,
    reply: str = FACTS,
) -> dict[str, Any]:
    return await extract_memories(
        worker_context(session, storage, llm=StubLLM(reply)),
        conversation_id=str(conversation_id),
        organization_id=str(organization_id),
        user_id=str(user_id) if user_id else None,
        task_id=str(task_id),
        agent_run_id=None,
    )


async def test_the_task_stores_what_the_conversation_taught_it(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    organization, owner, _ = await make_org_with_owner(db_session)
    conversation_id, task = await seed_conversation(
        db_session, organization_id=organization.id, user_id=owner.id
    )

    result = await run_task(
        db_session,
        storage,
        conversation_id=conversation_id,
        organization_id=organization.id,
        user_id=owner.id,
        task_id=task.id,
    )

    assert result["stored"] == 2
    stored = await MemoryRepository(db_session).list_for_organization(
        organization.id, user_id=owner.id
    )
    assert {memory.content for memory in stored} == {
        "Works in the Berlin office",
        "Approves invoices for their team",
    }


async def test_running_twice_stores_nothing_the_second_time(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """Idempotence, which this task gets from a constraint rather than from
    replacing anything. arq retries; a sweeper re-enqueues."""
    organization, owner, _ = await make_org_with_owner(db_session)
    conversation_id, task = await seed_conversation(
        db_session, organization_id=organization.id, user_id=owner.id
    )

    first = await run_task(
        db_session,
        storage,
        conversation_id=conversation_id,
        organization_id=organization.id,
        user_id=owner.id,
        task_id=task.id,
    )
    second = await run_task(
        db_session,
        storage,
        conversation_id=conversation_id,
        organization_id=organization.id,
        user_id=owner.id,
        task_id=task.id,
    )

    assert first["stored"] == 2
    assert second["stored"] == 0
    stored = await MemoryRepository(db_session).list_for_organization(
        organization.id, user_id=owner.id
    )
    assert len(stored) == 2


async def test_the_task_row_records_what_happened(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """Redis forgets a job an hour after it runs; this row is what answers "what
    did the background actually do on Tuesday?" — the reason `tasks` exists."""
    organization, owner, _ = await make_org_with_owner(db_session)
    conversation_id, task = await seed_conversation(
        db_session, organization_id=organization.id, user_id=owner.id
    )

    await run_task(
        db_session,
        storage,
        conversation_id=conversation_id,
        organization_id=organization.id,
        user_id=owner.id,
        task_id=task.id,
    )

    assert task.status is TaskStatus.SUCCEEDED
    assert task.attempts == 1
    assert task.result == {"stored": 2, "duplicates": 0, "reinforced": 0}


async def test_another_tenants_conversation_is_invisible_to_the_worker(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The worker is not exempt from tenancy just because nobody is watching. An
    unscoped `SELECT ... WHERE id = ?` here is the same bug as in a route,
    discovered later and by somebody else."""
    ours, our_owner, _ = await make_org_with_owner(db_session)
    theirs, their_owner, _ = await make_org_with_owner(db_session)
    conversation_id, task = await seed_conversation(
        db_session, organization_id=theirs.id, user_id=their_owner.id
    )

    result = await run_task(
        db_session,
        storage,
        conversation_id=conversation_id,
        organization_id=ours.id,
        user_id=our_owner.id,
        task_id=task.id,
    )

    assert result["stored"] == 0
    assert task.result == {"stored": 0, "reason": "conversation_gone"}


async def test_a_deleted_thread_is_not_a_failure(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The thread went away between the turn and the job. There is nothing to
    learn from and nothing to fix, so raising would only make arq retry a job
    that can never succeed."""
    organization, owner, _ = await make_org_with_owner(db_session)
    _, task = await seed_conversation(db_session, organization_id=organization.id, user_id=owner.id)

    result = await run_task(
        db_session,
        storage,
        conversation_id=uuid.uuid4(),
        organization_id=organization.id,
        user_id=owner.id,
        task_id=task.id,
    )

    assert result == {"stored": 0}


async def test_a_missing_task_row_does_not_stop_the_work(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The `tasks` row is a record for humans, not a precondition. Failing an
    extraction because its bookkeeping row was pruned would trade something
    valuable for something that is not."""
    organization, owner, _ = await make_org_with_owner(db_session)
    conversation_id, _ = await seed_conversation(
        db_session, organization_id=organization.id, user_id=owner.id
    )

    result = await run_task(
        db_session,
        storage,
        conversation_id=conversation_id,
        organization_id=organization.id,
        user_id=owner.id,
        task_id=uuid.uuid4(),
    )

    assert result["stored"] == 2
