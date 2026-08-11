# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M6)
"""Query → ranked chunks with citations. The heart of RAG.

embed query → ChunkRepository.similarity_search (org-scoped!) → optional
rerank. Consumed by /search, /ask, and the RAG agent's search_chunks tool.
"""

from __future__ import annotations

import uuid

from app.rag.embeddings import embed_query
from app.repositories.chunk_repository import ChunkRepository

# TODO(M6): class Retriever — retrieve(org_id, query, top_k) -> list[ScoredChunk]
# TODO(M8): reranking pass IF evals prove it helps (measure first)
