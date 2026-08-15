"""Memory vector search, dedup-aware writes, and the decay sweep.

Layer: repositories. Takes a session, owns no transaction. The counterpart to
`ChunkRepository` (M6), and it repeats that module's two hard-won pgvector
lessons because they are properties of the index, not of documents:

- **`vector_cosine_ops` must match the `<=>` operator.** A mismatch does not
  error; the planner silently stops using the index and recall gets slower
  forever.
- **HNSW filters *after* the vector scan.** Ask for the 3 nearest memories in an
  organization whose 40 nearest rows belong to other tenants and a plain index
  scan returns fewer than 3 — possibly none — with no error at all.
  `SET LOCAL hnsw.iterative_scan` makes pgvector keep searching until enough
  rows survive the filter.

What is different from chunks, and why it needed its own module
---------------------------------------------------------------
**Ranking is not distance.** A chunk is ranked by similarity alone. A memory is
ranked by similarity *blended with* importance and recency — and that blend
cannot be an `ORDER BY`, because the HNSW index only accelerates the distance
operator. Ordering by any expression containing `importance` throws the index
away and sequentially scans every memory in the organization.

So the query over-fetches by distance, which the index does serve, and the blend
re-ranks those candidates in Python. That is a genuine approximation: a memory
ranked 40th by similarity but very important will never be found. It is the
right trade, because the alternative degrades to a full scan exactly when the
table grows large enough for any of this to matter.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from sqlalchemy import ColumnElement, CursorResult, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.memory import Memory, MemoryScope

logger = structlog.get_logger(__name__)

MAX_TOP_K = 20
"""Ceiling on memories returned by one recall.

Far lower than `ChunkRepository.MAX_TOP_K` (50), deliberately. A retrieved chunk
carries a citation the user can check; a memory is an uncited assertion, and
twenty of them would crowd the documents out of the prompt while making the
answer *less* verifiable.
"""

CANDIDATE_MULTIPLIER = 4
"""How many rows to fetch by distance before re-ranking by the blend.

Four is a guess with a stated shape rather than a measurement: large enough that
importance and recency can meaningfully reorder the result, small enough that
the over-fetch stays cheap. Raising it approaches an exact blend at the cost of
approaching a full scan; the honest way to pick it is an eval over real
conversations, which this project does not yet have.
"""


@dataclass(frozen=True)
class ScoredMemory:
    """A recalled memory and every number that produced its rank.

    All three scores are carried, not only the final one. When a recall surfaces
    something odd, "was that similar, or merely important?" is the first
    question — and a single blended float cannot answer it. The same reasoning
    that puts `agent_steps` in front of users (ADR-0012): a system that shows its
    working can be argued with.
    """

    memory_id: uuid.UUID
    content: str
    scope: MemoryScope
    similarity: float
    importance: float
    recency: float
    score: float


def _visible_to(user_id: uuid.UUID | None) -> ColumnElement[bool]:
    """The scope predicate — the privacy boundary, written once.

    Org-scoped memories are visible to everyone in the organization; user-scoped
    ones only to their owner. Dropping the `user_id` half of this would not
    raise; it would surface one colleague's personal facts in another's answers,
    which is the worst failure this table can have and the direct analogue of
    M6's tenancy join.

    A caller with no `user_id` — a background job, the eval harness — sees org
    memories only. That is the safe direction: an unattributed caller gets less,
    never more. It lives in a module-level function rather than being spelled
    inline in four query methods, because four copies of a security predicate is
    three opportunities to omit a clause.
    """
    if user_id is None:
        return Memory.scope == MemoryScope.ORG

    return (Memory.scope == MemoryScope.ORG) | (
        (Memory.scope == MemoryScope.USER) & (Memory.user_id == user_id)
    )


class MemoryRepository:
    """Reads and writes for `memories`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_hash(
        self,
        organization_id: uuid.UUID,
        *,
        scope: MemoryScope,
        user_id: uuid.UUID | None,
        content_hash: str,
    ) -> Memory | None:
        """The existing row this fact would duplicate, if there is one.

        Matches the unique constraint exactly, `NULLS NOT DISTINCT` included:
        `user_id IS NULL` rather than `user_id == None`, because SQL's `= NULL`
        is never true and would turn every org-scoped lookup into a miss. A miss
        here means an insert, which the constraint then rejects — an
        `IntegrityError` raised in a background worker, on the one code path
        whose entire job is avoiding duplicates.
        """
        memory: Memory | None = await self._session.scalar(
            select(Memory).where(
                Memory.organization_id == organization_id,
                Memory.scope == scope,
                Memory.content_hash == content_hash,
                Memory.user_id.is_(None) if user_id is None else Memory.user_id == user_id,
            )
        )
        return memory

    async def add(self, memory: Memory) -> Memory:
        """Insert one memory. The caller owns the transaction."""
        self._session.add(memory)
        await self._session.flush()
        return memory

    async def nearest(
        self,
        organization_id: uuid.UUID,
        embedding: list[float],
        *,
        user_id: uuid.UUID | None,
        limit: int,
    ) -> list[tuple[Memory, float]]:
        """Candidate memories by cosine similarity, closest first."""
        if limit <= 0:
            message = f"limit must be positive, got {limit}"
            raise ValueError(message)

        # SET LOCAL, so it expires with this transaction rather than leaking into
        # whatever else reuses the pooled connection. See `ChunkRepository`.
        await self._session.execute(text("SET LOCAL hnsw.iterative_scan = 'strict_order'"))

        distance = Memory.embedding.cosine_distance(embedding).label("distance")

        rows = await self._session.execute(
            select(Memory, distance)
            .where(Memory.organization_id == organization_id, _visible_to(user_id))
            .order_by(distance)
            .limit(limit)
        )

        return [
            # Clamped for the reason `ChunkRepository` clamps: floating-point
            # error can put a distance a hair outside [0, 1], and a similarity of
            # 1.0000000002 reads as a bug to anyone looking at a response.
            (memory, max(0.0, min(1.0, 1.0 - float(memory_distance))))
            for memory, memory_distance in rows.all()
        ]

    async def touch(self, importances: dict[uuid.UUID, float]) -> None:
        """Record that these memories were recalled, and reinforce them.

        One statement per memory because each gets a different importance, and a
        `CASE` expression over a handful of rows is harder to read than the loop
        it replaces. `top_k` is capped at `MAX_TOP_K`, so this is bounded by
        construction.

        Writing on read makes recall a mutating operation — unusual enough to be
        worth stating twice (see `Memory.last_accessed_at`). It is what makes
        decay mean anything: without it `last_accessed_at` would only ever record
        when a memory was *written*, and the policy would be measuring age rather
        than use.
        """
        if not importances:
            return

        now = datetime.now(UTC)

        for memory_id, importance in importances.items():
            await self._session.execute(
                update(Memory)
                .where(Memory.id == memory_id)
                .values(last_accessed_at=now, importance=importance)
            )

        await self._session.flush()

    async def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        user_id: uuid.UUID | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Memory]:
        """A page of memories, most important first.

        Ordered by *stored* importance rather than decayed score, because decay
        depends on `now` and no index can. This listing is a debugging surface;
        recall is the ranked one.
        """
        return list(
            await self._session.scalars(
                select(Memory)
                .where(Memory.organization_id == organization_id, _visible_to(user_id))
                .order_by(Memory.importance.desc(), Memory.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )

    async def count_for_organization(
        self, organization_id: uuid.UUID, *, user_id: uuid.UUID | None = None
    ) -> int:
        total = await self._session.scalar(
            select(func.count(Memory.id)).where(
                Memory.organization_id == organization_id, _visible_to(user_id)
            )
        )
        return total or 0

    async def decay_candidates(
        self, organization_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, float, datetime]]:
        """The three columns `plan_maintenance` needs, and nothing else.

        Not `select(Memory)`. Every row carries a 1536-float embedding — roughly
        6 KB — so a sweep considering ten thousand memories would transfer 60 MB
        purely to decide which ones to delete. Three columns make the same
        decision on a few hundred kilobytes.
        """
        rows = await self._session.execute(
            select(Memory.id, Memory.importance, Memory.last_accessed_at).where(
                Memory.organization_id == organization_id
            )
        )
        return [(row[0], row[1], row[2]) for row in rows.all()]

    async def forget(self, memory_ids: list[uuid.UUID]) -> int:
        """Delete decayed memories. Returns how many rows went."""
        if not memory_ids:
            return 0

        result = cast(
            "CursorResult[Any]",
            await self._session.execute(delete(Memory).where(Memory.id.in_(memory_ids))),
        )
        await self._session.flush()

        logger.info("memory.forgotten", count=result.rowcount or 0)
        return result.rowcount or 0
