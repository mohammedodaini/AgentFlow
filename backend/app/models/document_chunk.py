"""`document_chunks` — the retrieval unit.

Layer: models. One row per slice of a document, with the vector that makes it
findable.

Why chunks are their own table
------------------------------
Retrieval returns *chunks*, and a citation needs the parent document, so the
join direction is chunk → document. Keeping them separate also makes
re-chunking a delete-and-reinsert against one table, with the document row and
its id untouched — which matters because the document id is what users bookmark
and what M7's answers cite.

Tenancy is deliberately absent from this table. There is no `organization_id`
column; a chunk belongs to a document, and the document belongs to an
organization. That is a real trade — see `ChunkRepository.similarity_search`,
which must join to `documents` on every query rather than filtering here — and
it is taken to keep one fact in one place. A denormalised copy that drifts is a
tenancy bug, and tenancy bugs are the expensive kind.
"""

from __future__ import annotations

import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

EMBEDDING_DIMENSIONS = 1536
"""The declared width of the vector column.

A constant rather than a literal because it must agree with
`Settings.embedding_dimensions`, and a test asserts exactly that. Changing it
is not a settings change — it is a migration plus a full re-embed of every
corpus, because a `vector(1536)` column rejects a 3072-dimension row outright.
"""


class DocumentChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One retrievable slice of one document."""

    __tablename__ = "document_chunks"

    __table_args__ = (
        # Two chunks cannot claim the same position in a document. This is what
        # makes re-ingestion safe to reason about: if the delete-then-insert in
        # the worker were ever interrupted halfway, the constraint fails loudly
        # instead of leaving a document with two chunk 0s and a retrieval layer
        # that returns whichever it happened to find first.
        UniqueConstraint("document_id", "chunk_index"),
        # The index that makes vector search fast. Declared here, in the model,
        # rather than only in the migration — and that distinction cost a bug.
        #
        # Creating it solely in the migration leaves `Base.metadata` unaware of
        # it, so `alembic check` reports the *database* as ahead of the models
        # and the next `--autogenerate` cheerfully emits `op.drop_index(...)`.
        # Someone would apply that migration, every query would silently fall
        # back to a sequential scan, and nothing would fail — search would just
        # get slower forever. `alembic check` caught it here instead.
        #
        # `vector_cosine_ops` must match the `<=>` operator in
        # ChunkRepository.similarity_search. A mismatch does not error either:
        # the planner simply never chooses the index.
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    """`CASCADE`, because a chunk without its document is unusable — it cannot
    be cited, and nothing can explain where its text came from.

    Not separately indexed: it is the leading column of the unique constraint
    above, so that index already serves `WHERE document_id = ?`. The same
    lesson M2 learned on `memberships`.
    """

    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    """Position within the document, from 0. Restores reading order, and lets a
    future "show me the surrounding context" feature find neighbours."""

    content: Mapped[str] = mapped_column(Text, nullable=False)
    """The text itself, stored rather than recomputed.

    It could be derived by re-parsing and re-chunking the original file. It is
    kept because every retrieval result needs it immediately — an answer that
    had to re-parse a 40-page PDF to quote one paragraph would be unusable —
    and because chunk boundaries would silently shift the day the chunker is
    tuned, so stored text is the only thing that keeps a citation stable.
    """

    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    """Tokens in `content`, measured at chunk time.

    Stored because M7 has to fit chunks into a context window and needs the
    number *before* deciding what to include. Counting at answer time would run
    the tokeniser over every candidate on every request.
    """

    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    """The vector this chunk is found by. See `__table_args__` for its index."""

    chunk_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    """Provenance for this slice: page number, section heading, chunker version.

    The attribute is `chunk_metadata` while the *column* is `metadata`, because
    `metadata` is reserved on a SQLAlchemy declarative class — it is the
    `MetaData` object itself, and shadowing it fails at class construction with
    an error that never mentions your column.
    """
