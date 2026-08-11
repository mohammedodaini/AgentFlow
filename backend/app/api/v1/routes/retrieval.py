# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M6)
"""/search (M6) and /ask (M7) — retrieval, then retrieval + Claude answer.

/search proves retrieval quality alone (tunable/testable without an LLM);
/ask adds generation with citations, streaming via SSE.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.rag.retrieval import Retriever
from app.schemas.document import AskRequest, AskResponse, SearchRequest, SearchResult

router = APIRouter(tags=["retrieval"])

# TODO(M6): POST /search — top-k chunks with scores + document citations
# TODO(M7): POST /ask — retrieve → prompt Claude → stream answer with sources
