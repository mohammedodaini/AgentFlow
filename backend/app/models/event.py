# ruff: noqa: F401  — remove once this module is implemented (M3)
"""`events` — append-only audit log: every login, connect, approval, send.

Actor is a user OR an agent run (both nullable FKs) — agents act too.
Compliance and debugging both read this. Future: partition by month.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# TODO(M3): class Event(Base) — organization_id FK, actor_user_id FK nullable,
#           actor_agent_run_id FK nullable, event_type, payload JSONB
