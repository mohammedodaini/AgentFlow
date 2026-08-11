# ruff: noqa: F401  — remove once this module is implemented (M6)
"""Text → vectors. The ONLY module that knows which embedding model we use.

Batched calls; model name + dimension come from settings (dimension must
match document_chunks.embedding vector(1536)).
"""

from __future__ import annotations

from app.core.config import get_settings

# TODO(M6): async embed_texts(texts: list[str]) -> list[list[float]] (batched)
# TODO(M6): async embed_query(text: str) -> list[float]
