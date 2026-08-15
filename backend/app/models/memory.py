"""`memories` — long-term agent memory: vector-searchable and decayable.

Layer: models. Structurally close to `document_chunks` (M6) — both carry a
`vector(1536)` and an HNSW index — and semantically its opposite.

**Documents are what the business uploaded. Memories are what the agent
learned.** That distinction is the reason these are two tables rather than one
with a `source` column, and it is not stylistic:

- A document chunk is *quoted*, with a citation the user can check. A memory is
  *asserted*, and there is nothing to click through to. They cannot be mixed in
  one ranked list without the citations becoming untrustworthy.
- A document is deleted when the business deletes it. A memory decays when it
  stops being used. One is a retention policy, the other is a scoring function.
- A document is authoritative. A memory is a model's summary of a conversation,
  which is to say it may simply be wrong — and a wrong memory quietly steers
  every future answer. That asymmetry deserves its own table, its own writer,
  and its own way of being switched off.

Scope
-----
`org` memories are facts about the company ("invoices are approved by Finance").
`user` memories are facts about one person ("prefers short answers"). The scope
is what stops a memory learned in a private thread leaking into a colleague's
answer, which is the failure mode that makes agent memory a privacy feature
rather than a convenience.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.document_chunk import EMBEDDING_DIMENSIONS

MAX_MEMORY_CHARS = 500
"""One memory is one fact, in one sentence.

The bound is a design statement, not a storage concern. A memory the length of a
paragraph is a summary, and summaries recalled into a prompt crowd out the
retrieved documents that carry citations. If something needs more than 500
characters it is a document, and belongs in the other table.
"""

CONTENT_HASH_LENGTH = 64
"""Hex-encoded SHA-256."""

DEFAULT_IMPORTANCE = 0.5
"""Where a newly extracted fact starts: neither trusted nor discounted.

Importance moves from here by *use* — recalled memories are reinforced — rather
than by a model's self-assessment of how important its own output was, which is
a number with no calibration behind it.
"""


class MemoryScope(enum.StrEnum):
    """Who a memory is about."""

    ORG = "org"
    USER = "user"


class Memory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One durable fact the agent learned."""

    __tablename__ = "memories"
    __table_args__ = (
        # The dedup guarantee, and the reason it needs a Postgres 15+ feature.
        #
        # `user_id` is NULL for org-scoped memories, and under the SQL standard
        # NULLs are distinct — so a plain UNIQUE constraint would happily accept
        # the *same* org fact a hundred times, once per extraction run, because
        # every row's NULL counts as unique. `NULLS NOT DISTINCT` makes NULL
        # compare equal to NULL here, which turns the constraint into the thing
        # it was always meant to be.
        #
        # The writer also skips near-duplicates by similarity. That is a policy
        # and can be wrong; this is a guarantee and cannot.
        UniqueConstraint(
            "organization_id",
            "scope",
            "user_id",
            "content_hash",
            name="uq_memories_organization_id_scope_user_id_content_hash",
            postgresql_nulls_not_distinct=True,
        ),
        # Recall filters by tenant and scope before ranking, so the filter
        # columns lead.
        Index("ix_memories_organization_id_scope", "organization_id", "scope"),
        # The vector index, declared in the model rather than only in the
        # migration — the distinction M6 paid for. An index that exists only in
        # the database leaves `Base.metadata` unaware of it, so the next
        # `--autogenerate` emits `op.drop_index(...)`; somebody applies it,
        # every recall falls back to a sequential scan, and nothing fails — it
        # just gets slower forever. `vector_cosine_ops` must match the `<=>`
        # operator in `MemoryRepository`, or the planner silently ignores it.
        Index(
            "ix_memories_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
        # Constraint names are bare here: `NAMING_CONVENTION` prepends
        # `ck_<table>_`, so spelling the prefix out produces
        # `ck_memories_ck_memories_importance`.
        CheckConstraint("importance >= 0 AND importance <= 1", name="importance_range"),
        # A user-scoped memory with no user is not a narrower memory, it is an
        # unreachable one: recall filters `user_id == the caller`, so the row
        # would be written, stored, decayed, and never once returned.
        CheckConstraint(
            "(scope = 'user' AND user_id IS NOT NULL) OR scope = 'org'",
            name="user_scope_has_user",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )

    scope: Mapped[MemoryScope] = mapped_column(
        Enum(
            MemoryScope,
            name="memory_scope",
            native_enum=True,
            # Stores "org"/"user", not "ORG"/"USER". Load-bearing twice over
            # here: the check constraint above compares against the *stored*
            # label, so without this it would compare 'ORG' to 'org' and reject
            # every row the application tried to write.
            values_callable=lambda enum_class: [member.value for member in enum_class],
        ),
        nullable=False,
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    """Whose memory, for `scope=user`.

    `CASCADE` here, unlike every other user foreign key in this schema, and the
    difference is deliberate. `agent_runs.triggered_by` is `SET NULL` because an
    audit record must outlive the person; a personal memory is not an audit
    record, it is a profile. When someone leaves, "prefers short answers" about
    them should leave with them.
    """

    content: Mapped[str] = mapped_column(String(MAX_MEMORY_CHARS), nullable=False)
    """The fact, in one sentence. `String` rather than `Text` so the length bound
    is enforced by the database and not only by whichever extractor happens to
    be writing today."""

    content_hash: Mapped[str] = mapped_column(String(CONTENT_HASH_LENGTH), nullable=False)
    """SHA-256 of the normalised content, for the uniqueness constraint above.

    A hash rather than the text itself because a unique index over 500-character
    strings is large and slow, while one over 64 fixed characters is neither.
    Normalisation (case, whitespace, trailing stop) happens in
    `app/memory/writer.py`, so "Invoices go to Finance." and "invoices go to
    finance" collide as they should.
    """

    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    """The same width as `document_chunks.embedding`, imported rather than
    repeated — one constant, so the day the model changes there is one number to
    change and not two that can drift apart."""

    importance: Mapped[float] = mapped_column(Float, nullable=False, default=DEFAULT_IMPORTANCE)
    """How much this memory should count, in [0, 1]. Reinforced by recall, eroded
    by time — see `app/memory/policies.py`."""

    last_accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """When recall last returned this memory.

    The recency half of the decay score. Written on *read*, which makes recall a
    mutating operation — unusual, deliberate, and the reason it is a service
    call rather than a bare repository query. A memory nothing ever recalls is
    one nobody needs, and this column is the only evidence of that.
    """

    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    """Which run this was extracted from — the provenance a wrong memory is
    diagnosed with. `SET NULL` so pruning traces does not delete what was learned
    from them."""
