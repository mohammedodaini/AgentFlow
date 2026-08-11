# ruff: noqa: F401  — remove once this module is implemented (M6)
"""Chunk + vector-search queries — the ONLY place that writes pgvector SQL.

Repository justified: similarity search with HNSW is real query complexity.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document_chunk import DocumentChunk

# TODO(M6): class ChunkRepository — bulk_insert(chunks), delete_for_document(doc_id),
#           similarity_search(org_id, embedding, top_k) -> chunks + distance
#           (join documents for tenancy filter + citation title)
