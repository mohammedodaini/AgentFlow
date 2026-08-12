"""Auth request/response shapes."""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr

from app.schemas.common import NormalizedEmail

MIN_PASSWORD_LENGTH = 12
"""OWASP's floor is 8; 12 is the length at which offline cracking stops being
cheap. Length beats composition rules — "Password1!" satisfies every
upper/lower/digit/symbol policy ever written and sits on the first page of
every wordlist, while a four-word passphrase satisfies none of them and is far
stronger. So: a length minimum, and no character-class rules at all."""

MAX_PASSWORD_LENGTH = 128
"""Argon2 has no length limit, which is the problem: hashing is deliberately
expensive, so an unbounded password field is a free denial-of-service — post a
10 MB "password" and the server burns CPU proving it wrong."""


class RegisterRequest(BaseModel):
    """Create an account. Also creates a personal organization — see AuthService."""

    email: NormalizedEmail
    password: SecretStr = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    full_name: str | None = Field(default=None, max_length=255)


class LoginRequest(BaseModel):
    """Exchange credentials for a token pair.

    No length constraints on the password here, deliberately. Rejecting a
    9-character login attempt as a *validation error* would tell an attacker
    that the password policy changed — and would lock out any user whose
    password predates the current minimum. Wrong credentials are wrong
    credentials: one answer, always 401.
    """

    email: NormalizedEmail
    password: SecretStr


class RefreshRequest(BaseModel):
    """Exchange a refresh token for a new pair.

    The token travels in the body rather than the `Authorization` header
    because that header carries *access* tokens everywhere else, and reusing it
    for a different credential type is how the two get confused — by proxies
    that log it, by middleware that validates it, and eventually by a person.
    """

    refresh_token: str


class TokenPairResponse(BaseModel):
    """What the client stores after register, login or refresh.

    `token_type` is fixed at "bearer" per RFC 6750 — clients read it to build
    the `Authorization: Bearer <token>` header rather than hardcoding the word.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 — the RFC 6750 scheme name, not a secret
