# ruff: noqa: F401  — remove once this module is implemented (M5)
"""`tasks` — DB mirror of arq queue jobs.

Redis holds the queue, Postgres holds the truth: users see progress,
ops can retry. Written by workers/, read by API status endpoints.
"""

from __future__ import annotations

import enum

from sqlalchemy import Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# TODO(M5): class TaskStatus(enum.StrEnum) — queued | running | succeeded | failed
# TODO(M5): class Task(Base) — organization_id FK, kind, payload JSONB, status,
#           attempts, result JSONB
