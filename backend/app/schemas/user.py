"""User API shapes. UserRead deliberately EXCLUDES password_hash (quiz Q1)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import APIModel, NormalizedEmail


class UserRead(APIModel):
    """A user as the API presents them.

    This class *is* the answer to "why not just return the ORM object?".
    `User` has a `password_hash` column. Serialising the model directly would
    publish an Argon2 digest of every user's password to anyone who can call
    `GET /users/me` — not catastrophic on its own, since Argon2 is what stands
    between a digest and a password, but it hands an attacker material to crack
    offline at their leisure.

    A schema is a whitelist. Add a column tomorrow and it stays private until
    somebody deliberately lists it here.
    """

    id: uuid.UUID
    email: NormalizedEmail
    full_name: str | None
    is_active: bool
    is_verified: bool
    created_at: datetime


class UserUpdate(BaseModel):
    """Partial update of the caller's own profile.

    Every field optional, because PATCH means "change what I sent, leave the
    rest". A schema with required fields would turn PATCH into PUT and quietly
    blank out anything the client did not resend.

    Notably absent: `email`, `is_active`, `is_verified`. Changing an email is
    not a profile edit — it needs a verification round trip to the new address,
    or an account takeover is one PATCH away. The other two are the system's
    opinion about the user, not the user's opinion about themselves.
    """

    full_name: str | None = Field(default=None, max_length=255)
