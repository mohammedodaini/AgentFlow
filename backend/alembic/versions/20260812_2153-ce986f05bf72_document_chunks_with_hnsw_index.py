"""document chunks with hnsw index

Revision ID: ce986f05bf72
Revises: 6bc020807cb8
Create Date: 2026-08-12 21:53:30.189191

M6. Adds `document_chunks` — the retrieval unit — plus the pgvector extension
it depends on and the HNSW index that makes searching it fast.

Heavily hand-edited, because autogenerate got three things wrong and each would
have failed differently:

1. **It emitted `pgvector.sqlalchemy.vector.VECTOR(...)` without importing
   `pgvector`.** A `NameError` on the first `alembic upgrade` — loud, at least,
   and caught here by ruff before it ever ran.
2. **It did not create the `vector` extension.** The dev database already had
   it, so autogenerate had no reason to notice. On a fresh database — a new
   contributor, or CI — the migration would fail on a type that does not exist.
   The dependency is real and belongs in the migration that needs it.
3. **It did not create the HNSW index.** The index is the entire point of the
   milestone, and it is *invisible* by omission: every query still returns
   correct rows, by sequential scan, so nothing fails. Search simply gets
   slower in proportion to the corpus — the kind of problem discovered in
   production, six months later, by a customer.

The first fix for (3) was a hand-written `op.execute("CREATE INDEX ...")`, and
that was wrong in an interesting way. It created the index in the database
while leaving `Base.metadata` unaware of it, so `alembic check` immediately
reported a pending `remove_index` — meaning the next `--autogenerate` would
have emitted a migration that *drops* the HNSW index. The index is now declared
on the model (`DocumentChunk.__table_args__`) and created here with
`op.create_index`, so the two agree and `alembic check` is clean.
"""

from __future__ import annotations

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "ce986f05bf72"
down_revision: str | None = "6bc020807cb8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HNSW_M = 16
"""Edges per node in the graph. Higher means better recall and a larger index.
16 is pgvector's default and a reasonable place to start."""

HNSW_EF_CONSTRUCTION = 64
"""Candidate list size while building. Higher means a better-connected graph
and a slower build — paid once, at migration time, rather than per query."""


def upgrade() -> None:
    """Apply the schema change."""
    # Hand-added. `vector(1536)` below is a type this extension defines, so the
    # ordering is not stylistic — the CREATE TABLE fails without it.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "document_chunks",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(dim=1536), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_chunks")),
        sa.UniqueConstraint(
            "document_id", "chunk_index", name=op.f("uq_document_chunks_document_id")
        ),
    )

    # `vector_cosine_ops` must match the `<=>` operator used in
    # ChunkRepository.similarity_search. If they disagree the index is simply
    # never chosen — no error, no warning, just a sequential scan forever.
    op.create_index(
        "ix_document_chunks_embedding_hnsw",
        "document_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_with={"m": HNSW_M, "ef_construction": HNSW_EF_CONSTRUCTION},
    )
    # Note for whoever adds the next vector index against a populated table:
    # this one is cheap because the table is empty. Building HNSW over millions
    # of existing rows takes an ACCESS EXCLUSIVE lock for the duration, which
    # means downtime — use CREATE INDEX CONCURRENTLY there, and remember it
    # cannot run inside Alembic's transaction.


def downgrade() -> None:
    """Reverse the schema change.

    The index goes with the table, so it needs no separate drop. The extension
    deliberately survives: it may predate this migration, other schemas may use
    it, and `DROP EXTENSION` would take their columns with it. Removing an
    extension is an operator's decision, not a migration's.
    """
    op.drop_table("document_chunks")
