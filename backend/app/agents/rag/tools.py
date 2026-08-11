# ruff: noqa: F401  — remove once this module is implemented (M9)
"""rag agent tools: search_chunks(), fetch_document() — wrap rag/retrieval + document service

Rule: every tool wraps a SERVICE method — tenancy, logging, and permissions
apply to agents automatically. Side-effect tools (*) raise the approval
interrupt before executing (M12).
"""

from __future__ import annotations

from langchain_core.tools import tool

# TODO(M9): implement the tools listed above
