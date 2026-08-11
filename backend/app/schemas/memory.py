# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M10)
"""Memory API shapes (admin/debug visibility into what the agent remembers)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from app.schemas.common import APIModel

# TODO(M10): MemoryRead — id, scope, content, importance, last_accessed_at
