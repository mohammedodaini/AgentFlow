# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M10)
"""`messages` — APPEND-ONLY chat turns. Editing history destroys auditability.

agent_run_id (nullable) links an assistant message to the run that produced
it — the bridge between chat UX and the execution trace.
"""

from __future__ import annotations

import enum

from sqlalchemy import Enum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# TODO(M10): class MessageRole(enum.StrEnum) — user | assistant | tool
# TODO(M10): class Message(Base) — conversation_id FK, role, content,
#            agent_run_id FK nullable, token_usage JSONB
