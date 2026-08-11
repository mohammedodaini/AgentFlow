# ruff: noqa: F401  — remove once this module is implemented (M5)
"""arq task: ingest_document(document_id) — the slow half of the 202 pattern.

parse → chunk → embed → store chunks → status=ready (or failed + error).
Updates the `tasks` mirror row so users can poll progress.
"""

from __future__ import annotations

import uuid

from app.rag import chunking, embeddings, ingestion

# TODO(M5): async def ingest_document(ctx, document_id) — idempotent (safe to retry)
