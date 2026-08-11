# ruff: noqa: F401  — remove once this module is implemented (M10)
"""Memory vector search + decay updates. Repository justified: vector search
plus importance/recency scoring in SQL."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.memory import Memory

# TODO(M10): class MemoryRepository — upsert(memory), search(org_id, embedding, top_k,
#            scope), touch_last_accessed(ids), decay_pass()
