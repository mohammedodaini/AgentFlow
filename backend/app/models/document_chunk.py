# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M6)
"""`document_chunks` — the retrieval unit. embedding vector(1536), HNSW index.

Own table so retrieval returns chunks (join up for citations) and re-chunking
is delete+reinsert without touching documents. Unique (document_id, chunk_index).
"""

from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

# TODO(M6): class DocumentChunk(Base) — document_id FK (ondelete=CASCADE), chunk_index,
#           content, token_count, embedding Vector(1536), metadata JSONB
# TODO(M6): HNSW index on embedding (created in the Alembic migration, cosine ops)
