# ruff: noqa: F401  — remove once this module is implemented (M2)
"""`users` table — auth identity ONLY; business data hangs off organizations.

Imported by: auth/, services/user_service, models/membership (relationship).
"""

from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

# TODO(M2): class User(Base) — email (unique, indexed), password_hash, full_name,
#           is_active, is_verified; relationship memberships -> Membership
