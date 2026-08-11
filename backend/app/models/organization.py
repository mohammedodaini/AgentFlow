# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M2)
"""`organizations` table — THE tenant. Almost every other table FKs here.

Multi-tenancy rule: queries are scoped by organization_id, never by user_id.
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

# TODO(M2): class Organization(Base) — name, slug (unique), plan;
#           relationship memberships -> Membership
