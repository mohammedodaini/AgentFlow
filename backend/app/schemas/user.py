# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M3)
"""User API shapes. UserRead deliberately EXCLUDES password_hash (quiz Q1)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, EmailStr

from app.schemas.common import APIModel

# TODO(M3): UserRead — id, email, full_name, is_active, is_verified
# TODO(M3): UserUpdate — full_name (partial update semantics)
