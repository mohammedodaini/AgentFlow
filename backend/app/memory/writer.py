# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M10)
"""Extract durable facts from a finished run and store them (async, via the
memory worker task — never inline with a user response)."""

from __future__ import annotations

from app.rag.embeddings import embed_texts
from app.repositories.memory_repository import MemoryRepository

# TODO(M10): extract_and_store(run_id) — LLM extraction pass -> embed ->
#            upsert (update importance instead of duplicating near-identical facts)
