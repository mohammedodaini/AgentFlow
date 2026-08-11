# ruff: noqa: F401  — remove once this module is implemented (M5)
"""Document → text: parsing per mime type (PDF first).

Called by workers/tasks/ingestion.py, never in a request. Pipeline:
parse (here) → chunk (chunking.py) → embed (embeddings.py) → store (chunk repo).
"""

from __future__ import annotations

# TODO(M5): extract_text(storage_uri, mime_type) -> str — pypdf first; raise
#           DocumentIngestionError with actionable message on failure
