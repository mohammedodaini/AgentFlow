# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M2)
"""`memberships` — users ⟷ organizations many-to-many WITH a role payload.

A real table (not a bare join): roles, invites, and seat-billing live here
later. Unique on (user_id, organization_id).
"""

from __future__ import annotations

import enum

from sqlalchemy import Enum, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

# TODO(M2): class Role(enum.StrEnum) — owner | admin | member
# TODO(M2): class Membership(Base) — user_id FK, organization_id FK, role
