"""Refresh-token lifecycle: rotation and revocation (jti denylist in Redis).

Separate from core/security (pure JWT encode/decode) because rotation policy
is a flow decision, not a primitive.

The asymmetry this module exists to manage
------------------------------------------
Access tokens are **stateless**: valid until they expire, and nothing can call
them back. Checking a denylist on every request would put a Redis round trip in
front of every single endpoint, which is the cost JWTs were adopted to avoid.
The mitigation is time — 30 minutes, so a stolen access token has a short life.

Refresh tokens are **revocable**, because they live for a week and are the
credential worth stealing. Every refresh rotates: the presented token is
revoked and a new pair issued. So a refresh token is single-use, and a stolen
one is only useful until the real user next refreshes.

What the denylist costs is bounded by construction: each entry expires exactly
when the token it denies would have expired anyway, so the key space can never
outgrow the number of tokens currently alive.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import structlog
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError
from app.core.security import create_token, decode_token

logger = structlog.get_logger(__name__)

REVOKED_KEY_PREFIX = "revoked:jti:"
"""Namespaced so `KEYS revoked:jti:*` is possible during an incident, and so
the denylist can never collide with arq's queue keys in the same database."""


class TokenPair(NamedTuple):
    """What every successful auth flow returns."""

    access_token: str
    refresh_token: str


def _revoked_key(jti: str) -> str:
    return f"{REVOKED_KEY_PREFIX}{jti}"


class TokenService:
    """Issues, rotates and revokes tokens.

    A class rather than loose functions because every method needs the Redis
    client, and threading it through eight call sites by hand is how one of
    them ends up reaching for a global instead.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    def issue_pair(self, user_id: uuid.UUID) -> TokenPair:
        """Mint a fresh access + refresh pair for a user.

        Not async: minting is pure CPU work. Only revocation touches Redis.
        """
        settings = get_settings()
        subject = str(user_id)

        return TokenPair(
            access_token=create_token(
                subject,
                token_type="access",  # noqa: S106 — a token *kind*, not a secret
                expires_in=timedelta(minutes=settings.access_token_expire_minutes),
            ),
            refresh_token=create_token(
                subject,
                token_type="refresh",  # noqa: S106 — a token *kind*, not a secret
                expires_in=timedelta(days=settings.refresh_token_expire_days),
            ),
        )

    async def rotate(self, refresh_token: str) -> TokenPair:
        """Exchange a refresh token for a new pair, invalidating the old one.

        Rotation is what makes theft detectable and survivable. Because each
        refresh token works exactly once, a stolen one stops working the moment
        the legitimate user refreshes — and an attempt to reuse an already-spent
        token is a signal that something has gone wrong, which is why the replay
        path logs a warning rather than failing quietly.

        Raises `AuthenticationError` if the token is invalid, expired, of the
        wrong type, or already used.
        """
        claims = decode_token(refresh_token, expected_type="refresh")
        jti = str(claims["jti"])
        subject = str(claims["sub"])

        if await self.is_revoked(jti):
            # Either a replayed token or a race between two browser tabs. Both
            # are worth seeing; only the first is worth worrying about.
            logger.warning("auth.refresh_token_replayed", jti=jti, user_id=subject)
            message = "Could not validate credentials"
            raise AuthenticationError(message)

        await self._revoke_claims(claims)

        return self.issue_pair(uuid.UUID(subject))

    async def revoke(self, refresh_token: str) -> None:
        """Invalidate a refresh token — the honest half of logout.

        Deliberately silent about tokens that are already invalid. Logout has to
        be idempotent: a client retrying after a dropped response, or sending a
        token that expired an hour ago, has still achieved what it asked for.
        Returning an error there would also tell an attacker which tokens are
        real.
        """
        try:
            claims = decode_token(refresh_token, expected_type="refresh")
        except AuthenticationError:
            return

        await self._revoke_claims(claims)

    async def is_revoked(self, jti: str) -> bool:
        """Whether this token id has been denied."""
        return bool(await self._redis.exists(_revoked_key(jti)))

    async def _revoke_claims(self, claims: dict[str, Any]) -> None:
        """Write the denylist entry, expiring it when the token would expire.

        The TTL is the whole design. A denylist keyed by token id would grow
        forever; one whose entries die with their tokens stays proportional to
        live sessions and needs no cleanup job that someone has to remember.
        """
        remaining = self._seconds_until_expiry(claims)

        if remaining <= 0:
            # `decode_token` would have rejected an expired token, but one
            # expiring between that call and this line is a real, if unlikely,
            # race. Nothing to deny — it is already dead.
            return

        await self._redis.set(_revoked_key(str(claims["jti"])), "1", ex=remaining)

    @staticmethod
    def _seconds_until_expiry(claims: dict[str, Any]) -> int:
        """Whole seconds left on a token, rounded up.

        Rounded up rather than down so the denylist entry always outlives the
        token; rounding the other way leaves a sub-second window in which a
        revoked token is briefly valid again.
        """
        expires_at = datetime.fromtimestamp(float(claims["exp"]), tz=UTC)
        return math.ceil((expires_at - datetime.now(UTC)).total_seconds())
