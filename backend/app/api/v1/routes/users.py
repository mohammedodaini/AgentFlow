"""GET/PATCH /users/me — the current user's profile."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser
from app.db.deps import get_db
from app.schemas.user import UserRead, UserUpdate
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", summary="The authenticated user's profile")
async def read_me(user: CurrentUser) -> UserRead:
    """Return the caller's own profile.

    `get_current_user` already loaded the row, so there is nothing left to
    fetch — but the conversion to `UserRead` still matters, and is the point of
    this endpoint as an example. `User` carries `password_hash`; `UserRead`
    does not. Returning `user` directly would publish an Argon2 digest of the
    caller's password on every profile load.

    Note there is no `/users/{id}`. Reading *other* people's profiles is an
    organization-scoped question — "who is in my workspace?" — and it is
    answered by `GET /organizations/{id}/members`, where the tenancy check
    lives.
    """
    return UserRead.model_validate(user)


@router.patch("/me", summary="Update the authenticated user's profile")
async def update_me(
    changes: UserUpdate,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> UserRead:
    """Apply a partial update to the caller's own profile.

    PATCH rather than PUT: the client sends only what changed. `UserUpdate`
    accepts `full_name` and nothing else — email changes need a verification
    round trip to the new address, and `is_active` / `is_verified` are the
    system's judgement about the user rather than the user's about themselves.

    The route cannot be tricked into editing somebody else: the id comes from
    the token via `CurrentUser`, and there is no parameter that could override
    it.
    """
    updated = await UserService(session).update_profile(user.id, changes)
    return UserRead.model_validate(updated)
