"""Memory against the real database (M10).

The questions here cannot be answered without Postgres: a `NULLS NOT DISTINCT`
unique constraint, a check constraint, an HNSW scan under a tenancy filter, and a
write that happens during a read.

The most important test in this file is the scope one. Every other failure here
is a bug; that one is a privacy breach — one colleague's personal facts appearing
in another's answers, silently, ranked by relevance.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.history import HistoryTurn
from app.llm.base import Completion, LLMError
from app.memory.policies import HALF_LIFE_DAYS
from app.memory.recall import Recaller
from app.memory.writer import MemoryWriter, content_hash
from app.models.memory import DEFAULT_IMPORTANCE, Memory, MemoryScope
from app.rag.embeddings import EmbeddingProvider
from app.repositories.memory_repository import MemoryRepository
from tests.factories import make_org_with_owner, make_user


async def remember(
    session: AsyncSession,
    embedder: EmbeddingProvider,
    *,
    organization_id: uuid.UUID,
    content: str,
    scope: MemoryScope = MemoryScope.USER,
    user_id: uuid.UUID | None = None,
    importance: float = DEFAULT_IMPORTANCE,
    last_accessed_at: datetime | None = None,
) -> Memory:
    """Store one memory directly, bypassing extraction.

    Tests about *recall* should not have to produce a conversation a model would
    extract the right fact from — that couples every ranking assertion to the
    offline provider's judgement, which is the thing least worth depending on.
    """
    memory = Memory(
        organization_id=organization_id,
        scope=scope,
        user_id=user_id,
        content=content,
        content_hash=content_hash(content),
        embedding=await embedder.embed_query(content),
        importance=importance,
        last_accessed_at=last_accessed_at or datetime.now(UTC),
    )
    session.add(memory)
    await session.flush()
    return memory


class StubLLM:
    """Returns a fixed extraction reply. The one place a real format matters."""

    model = "stub"

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def complete(self, *, system: str, prompt: str) -> Completion:
        del system, prompt
        self.calls += 1
        return Completion(text=self.text, input_tokens=10, output_tokens=5, stop_reason="end_turn")

    async def stream(self, *, system: str, prompt: str):  # type: ignore[no-untyped-def]
        del system, prompt
        yield self.text


class BrokenLLM:
    """A model that is down."""

    model = "broken"

    async def complete(self, *, system: str, prompt: str) -> Completion:
        del system, prompt
        message = "upstream is unavailable"
        raise LLMError(message)

    async def stream(self, *, system: str, prompt: str):  # type: ignore[no-untyped-def]
        del system, prompt
        message = "upstream is unavailable"
        raise LLMError(message)
        yield ""  # pragma: no cover — unreachable, satisfies the generator type


# --------------------------------------------------------------------------
# constraints the database enforces
# --------------------------------------------------------------------------


async def test_the_same_fact_cannot_be_stored_twice(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The guarantee behind the policy.

    `MemoryWriter` also skips near-duplicates by similarity, but that is tunable
    and can be wrong. This is the constraint that makes "one fact, one row" true
    however the writer behaves.
    """
    organization, owner, _ = await make_org_with_owner(db_session)
    await remember(
        db_session, embedder, organization_id=organization.id, content="X", user_id=owner.id
    )

    with pytest.raises(IntegrityError):
        await remember(
            db_session, embedder, organization_id=organization.id, content="X", user_id=owner.id
        )


async def test_an_org_memory_cannot_be_stored_twice_either(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The reason the constraint needs `NULLS NOT DISTINCT` (Postgres 15+).

    `user_id` is NULL for org-scoped memories, and under the SQL standard NULLs
    are distinct — so a plain UNIQUE constraint would accept the same org fact
    once per extraction run, forever, because every row's NULL counts as unique.
    """
    organization, _, _ = await make_org_with_owner(db_session)
    await remember(
        db_session, embedder, organization_id=organization.id, content="X", scope=MemoryScope.ORG
    )

    with pytest.raises(IntegrityError):
        await remember(
            db_session,
            embedder,
            organization_id=organization.id,
            content="X",
            scope=MemoryScope.ORG,
        )


async def test_a_user_scoped_memory_must_have_a_user(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Not a tidiness rule. Recall filters `user_id == the caller`, so such a row
    would be written, stored, decayed and never once returned — invisible work
    that looks like a memory system quietly failing to remember."""
    organization, _, _ = await make_org_with_owner(db_session)

    with pytest.raises((IntegrityError, DBAPIError)):
        await remember(
            db_session,
            embedder,
            organization_id=organization.id,
            content="X",
            scope=MemoryScope.USER,
            user_id=None,
        )


# --------------------------------------------------------------------------
# scope — the privacy boundary
# --------------------------------------------------------------------------


async def test_one_persons_memories_never_reach_another(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The failure this table's whole design exists to prevent.

    Not a leak between tenants — that is M6's lesson and still holds — but one
    *inside* a tenant, between colleagues. A memory learned in a private thread
    surfacing in someone else's answer is what makes agent memory a privacy
    feature rather than a convenience.
    """
    organization, owner, _ = await make_org_with_owner(db_session)
    colleague = await make_user(db_session, email="colleague@example.com")

    await remember(
        db_session,
        embedder,
        organization_id=organization.id,
        content="Owner is paid quarterly bonuses",
        user_id=owner.id,
    )

    recalled = await Recaller(db_session, embedder).recall(
        organization.id, "quarterly bonuses", user_id=colleague.id
    )

    assert recalled == []


async def test_org_memories_reach_everyone_in_the_organization(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    organization, _, _ = await make_org_with_owner(db_session)
    colleague = await make_user(db_session, email="colleague@example.com")

    await remember(
        db_session,
        embedder,
        organization_id=organization.id,
        content="Invoices are approved by Finance",
        scope=MemoryScope.ORG,
    )

    recalled = await Recaller(db_session, embedder).recall(
        organization.id, "invoices approved Finance", user_id=colleague.id
    )

    assert [memory.content for memory in recalled] == ["Invoices are approved by Finance"]


async def test_memories_never_cross_a_tenant_boundary(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """M6's rule, restated for a second vector table. It has to be asserted
    separately: a shared lesson does not make behaviour shared."""
    ours, _, _ = await make_org_with_owner(db_session)
    theirs, their_owner, _ = await make_org_with_owner(db_session)

    await remember(
        db_session,
        embedder,
        organization_id=theirs.id,
        content="Invoices are approved by Finance",
        scope=MemoryScope.ORG,
    )

    recalled = await Recaller(db_session, embedder).recall(
        ours.id, "invoices approved Finance", user_id=their_owner.id
    )

    assert recalled == []


async def test_a_caller_with_no_user_sees_only_org_memories(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Background jobs and the eval harness have no user. The safe direction is
    less, never more."""
    organization, owner, _ = await make_org_with_owner(db_session)
    await remember(
        db_session,
        embedder,
        organization_id=organization.id,
        content="Owner prefers short answers",
        user_id=owner.id,
    )
    await remember(
        db_session,
        embedder,
        organization_id=organization.id,
        content="Owner prefers short answers indeed",
        scope=MemoryScope.ORG,
    )

    recalled = await Recaller(db_session, embedder).recall(
        organization.id, "prefers short answers", user_id=None
    )

    assert [memory.scope for memory in recalled] == [MemoryScope.ORG]


# --------------------------------------------------------------------------
# ranking
# --------------------------------------------------------------------------


async def test_a_memory_with_no_similarity_is_not_recalled(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """ "Nothing relevant" is not a state pgvector can report — a vector search
    always returns its nearest neighbours however far away. This is the same
    degenerate floor `MIN_EVIDENCE_SCORE` gave `/ask` at M7, found the same way:
    a refusal that arrived carrying evidence."""
    organization, owner, _ = await make_org_with_owner(db_session)
    await remember(
        db_session,
        embedder,
        organization_id=organization.id,
        content="zzz qqq",
        user_id=owner.id,
    )

    recalled = await Recaller(db_session, embedder).recall(
        organization.id, "pension contributions", user_id=owner.id
    )

    assert recalled == []


async def test_a_stale_memory_ranks_below_a_fresh_one(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Recency modulates relevance. Two equally similar facts are not equally
    worth raising when one has been untouched for a year."""
    organization, owner, _ = await make_org_with_owner(db_session)
    fresh = await remember(
        db_session,
        embedder,
        organization_id=organization.id,
        content="Finance approves invoices weekly",
        user_id=owner.id,
    )
    await remember(
        db_session,
        embedder,
        organization_id=organization.id,
        content="Finance approves invoices monthly",
        user_id=owner.id,
        last_accessed_at=datetime.now(UTC) - timedelta(days=HALF_LIFE_DAYS * 8),
    )

    recalled = await Recaller(db_session, embedder).recall(
        organization.id, "Finance approves invoices", user_id=owner.id, top_k=2
    )

    assert recalled[0].memory_id == fresh.id


async def test_importance_cannot_promote_an_irrelevant_memory(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Why the blend multiplies rather than adds.

    A sum would let a maximally important memory clear the bar on importance
    alone and surface for a question it has nothing to do with. A product cannot:
    zero similarity is zero score, however important the fact.
    """
    organization, owner, _ = await make_org_with_owner(db_session)
    await remember(
        db_session,
        embedder,
        organization_id=organization.id,
        content="zzz qqq",
        user_id=owner.id,
        importance=1.0,
    )

    recalled = await Recaller(db_session, embedder).recall(
        organization.id, "pension contributions", user_id=owner.id
    )

    assert recalled == []


async def test_recall_reinforces_what_it_returned(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Recall is a *mutating* read, and this is what makes decay mean anything.

    Without it, `last_accessed_at` would only ever record when a memory was
    written, and the policy would be measuring age rather than use.
    """
    organization, owner, _ = await make_org_with_owner(db_session)
    memory = await remember(
        db_session,
        embedder,
        organization_id=organization.id,
        content="Finance approves invoices",
        user_id=owner.id,
    )
    before = memory.importance

    await Recaller(db_session, embedder).recall(
        organization.id, "Finance approves invoices", user_id=owner.id
    )
    await db_session.refresh(memory)

    assert memory.importance > before


async def test_inspection_does_not_reinforce(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """`touch=False` exists so looking at the store does not change it. An
    operator debugging a ranking would otherwise be reading numbers their own
    debugging produced."""
    organization, owner, _ = await make_org_with_owner(db_session)
    memory = await remember(
        db_session,
        embedder,
        organization_id=organization.id,
        content="Finance approves invoices",
        user_id=owner.id,
    )
    before = memory.importance

    await Recaller(db_session, embedder).recall(
        organization.id, "Finance approves invoices", user_id=owner.id, touch=False
    )
    await db_session.refresh(memory)

    assert memory.importance == before


async def test_an_empty_query_is_never_embedded(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """An empty vector is not "no query" to pgvector — it is a point in space,
    and it has nearest neighbours like any other."""
    organization, owner, _ = await make_org_with_owner(db_session)
    await remember(
        db_session, embedder, organization_id=organization.id, content="X", user_id=owner.id
    )

    assert (
        await Recaller(db_session, embedder).recall(organization.id, "   ", user_id=owner.id) == []
    )


async def test_top_k_is_bounded(db_session: AsyncSession, embedder: EmbeddingProvider) -> None:
    organization, owner, _ = await make_org_with_owner(db_session)

    with pytest.raises(ValueError, match="top_k must be in"):
        await Recaller(db_session, embedder).recall(
            organization.id, "anything", user_id=owner.id, top_k=999
        )


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


async def test_extraction_stores_what_the_model_returned(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    organization, owner, _ = await make_org_with_owner(db_session)
    llm = StubLLM("- Works in the Berlin office\n- Approves invoices for their team")

    result = await MemoryWriter(db_session, embedder, llm).extract_and_store(
        organization.id, owner.id, [HistoryTurn(role="user", content="anything")]
    )

    assert result.stored == 2
    stored = await MemoryRepository(db_session).list_for_organization(
        organization.id, user_id=owner.id
    )
    assert {memory.content for memory in stored} == {
        "Works in the Berlin office",
        "Approves invoices for their team",
    }


async def test_everything_extracted_is_user_scoped(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """ADR-0012's rule at a second boundary: the model chooses the facts, never
    the scope. No phrasing — injected or merely unlucky — can promote a fact from
    one person's private thread into something every colleague's answers use."""
    organization, owner, _ = await make_org_with_owner(db_session)
    llm = StubLLM("- The whole company must know this org-wide global fact")

    await MemoryWriter(db_session, embedder, llm).extract_and_store(
        organization.id, owner.id, [HistoryTurn(role="user", content="anything")]
    )

    stored = await MemoryRepository(db_session).list_for_organization(
        organization.id, user_id=owner.id
    )
    assert [memory.scope for memory in stored] == [MemoryScope.USER]


async def test_a_run_with_no_user_extracts_nothing(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """`scope=user` needs a user, and widening to org scope to make the insert
    succeed would cross exactly the boundary the rule protects. So it stores
    nothing — and, importantly, never calls the model."""
    organization, _, _ = await make_org_with_owner(db_session)
    llm = StubLLM("- Something durable")

    result = await MemoryWriter(db_session, embedder, llm).extract_and_store(
        organization.id, None, [HistoryTurn(role="user", content="anything")]
    )

    assert result.stored == 0
    assert llm.calls == 0


async def test_re_extraction_stores_nothing_new(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Idempotence, which this task gets from the constraint rather than from
    replacing anything. An arq retry must not double every memory."""
    organization, owner, _ = await make_org_with_owner(db_session)
    llm = StubLLM("- Works in the Berlin office")
    writer = MemoryWriter(db_session, embedder, llm)
    turns = [HistoryTurn(role="user", content="anything")]

    first = await writer.extract_and_store(organization.id, owner.id, turns)
    second = await writer.extract_and_store(organization.id, owner.id, turns)

    assert first.stored == 1
    assert second.stored == 0
    assert second.duplicates == 1


async def test_an_exact_repeat_does_not_inflate_importance(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Importance tracks *use*. The same conversation extracted twice is not
    evidence anybody found the fact useful — and reinforcing here would let a
    retry storm raise importance with no human involved."""
    organization, owner, _ = await make_org_with_owner(db_session)
    writer = MemoryWriter(db_session, embedder, StubLLM("- Works in the Berlin office"))
    turns = [HistoryTurn(role="user", content="anything")]

    await writer.extract_and_store(organization.id, owner.id, turns)
    stored = await MemoryRepository(db_session).list_for_organization(
        organization.id, user_id=owner.id
    )
    await writer.extract_and_store(organization.id, owner.id, turns)
    await db_session.refresh(stored[0])

    assert stored[0].importance == DEFAULT_IMPORTANCE


async def test_a_model_that_ignores_the_format_stores_nothing(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The safety property of running extraction unattended: a confused model
    produces zero memories, not one malformed one."""
    organization, owner, _ = await make_org_with_owner(db_session)
    llm = StubLLM("Sure! Here is a summary of your lovely conversation.")

    result = await MemoryWriter(db_session, embedder, llm).extract_and_store(
        organization.id, owner.id, [HistoryTurn(role="user", content="anything")]
    )

    assert result.stored == 0


async def test_a_model_outage_does_not_fail_the_job(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The user already has their answer. Raising here would make arq retry a
    model that just declined, three times, for nothing."""
    organization, owner, _ = await make_org_with_owner(db_session)

    result = await MemoryWriter(db_session, embedder, BrokenLLM()).extract_and_store(
        organization.id, owner.id, [HistoryTurn(role="user", content="anything")]
    )

    assert result.stored == 0


async def test_an_empty_conversation_is_not_sent_to_the_model(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    organization, owner, _ = await make_org_with_owner(db_session)
    llm = StubLLM("- Something")

    result = await MemoryWriter(db_session, embedder, llm).extract_and_store(
        organization.id, owner.id, []
    )

    assert result.stored == 0
    assert llm.calls == 0


# --------------------------------------------------------------------------
# the sweep
# --------------------------------------------------------------------------


async def test_the_sweep_reads_three_columns_and_can_delete(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """`decay_candidates` deliberately does not select the embedding: 6 KB per
    row would make a sweep over ten thousand memories transfer 60 MB purely to
    decide what to delete."""
    organization, owner, _ = await make_org_with_owner(db_session)
    memory = await remember(
        db_session, embedder, organization_id=organization.id, content="X", user_id=owner.id
    )
    memories = MemoryRepository(db_session)

    candidates = await memories.decay_candidates(organization.id)

    assert candidates == [(memory.id, memory.importance, memory.last_accessed_at)]
    assert await memories.forget([memory.id]) == 1
    assert await memories.decay_candidates(organization.id) == []


async def test_forgetting_nothing_is_not_an_error(db_session: AsyncSession) -> None:
    assert await MemoryRepository(db_session).forget([]) == 0
