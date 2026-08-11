# ruff: noqa: F401  — remove once this module is implemented (M10)
"""`memories` — long-term agent memory, vector-searchable, decayable.

Distinct from RAG documents: documents are what the BUSINESS uploaded,
memories are what the AGENT learned. scope: org-wide or per-user.
"""

from __future__ import annotations

import enum
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Enum, Float, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# TODO(M10): class MemoryScope(enum.StrEnum) — org | user
# TODO(M10): class Memory(Base) — organization_id FK, scope, user_id FK nullable,
#            content, embedding Vector(1536), importance, last_accessed_at,
#            source_run_id FK agent_runs
