# ruff: noqa: F401  — remove once this module is implemented (M10)
"""Retrieve memories relevant to the current context; feeds the supervisor's
recall_memory() tool. Touches last_accessed_at (recency feeds decay)."""

from __future__ import annotations

from app.rag.embeddings import embed_query
from app.repositories.memory_repository import MemoryRepository

# TODO(M10): recall(org_id, user_id, context, top_k) — vector similarity
#            blended with importance + recency
