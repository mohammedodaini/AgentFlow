# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M10)
"""`conversations` — a chat thread owned by a user within an org."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

# TODO(M10): class Conversation(Base) — organization_id FK, user_id FK, title,
#            archived_at (nullable); relationship messages -> Message
