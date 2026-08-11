# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M3)
"""User business logic (profile reads/updates). Registration lives in
app/auth/service.py because it is an auth flow (tokens, hashing)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.user import User

# TODO(M3): class UserService — get_profile(user_id), update_profile(user_id, changes)
