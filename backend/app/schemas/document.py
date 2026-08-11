# ruff: noqa: F401  — remove once this module is implemented (M5)
"""Document + retrieval API shapes (documents at M5, search/ask at M6/M7)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from app.schemas.common import APIModel

# TODO(M5): DocumentRead — id, title, source, mime_type, status, error, created_at
# TODO(M6): SearchRequest — query, top_k · SearchResult — chunk content, score,
#           document_id, document_title (the citation)
# TODO(M7): AskRequest — question · AskResponse — answer, sources[SearchResult], usage
