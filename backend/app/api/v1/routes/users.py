# ruff: noqa: F401  — remove once this module is implemented (M3)
"""GET/PATCH /users/me — the current user's profile."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.schemas.user import UserRead, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])

# TODO(M3): GET /users/me — returns UserRead (never the ORM object — see quiz Q1)
# TODO(M3): PATCH /users/me — update full_name etc.
