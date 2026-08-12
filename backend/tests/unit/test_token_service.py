"""Refresh-token rotation and revocation (M4).

The end-to-end suite proves these flows work over HTTP. What it cannot show is
*how* revocation is stored — and the storage detail is the whole design,
because a denylist whose entries outlive their tokens grows without bound and
needs a cleanup job nobody remembers to run.

So these tests inspect the Redis calls directly, through a stub that records
them. That is deliberate coupling to an implementation detail: the TTL is not
an incidental choice, it is the property that makes the denylist safe.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.auth.tokens import REVOKED_KEY_PREFIX, TokenService
from app.core.config import get_settings
from app.core.exceptions import AuthenticationError
from app.core.security import decode_token

CLOCK_TOLERANCE_SECONDS = 5


class FakeRedis:
    """An in-memory stand-in that records the TTL each key was set with."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}

    async def exists(self, key: str) -> int:
        return int(key in self.values)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value
        if ex is not None:
            self.expiries[key] = ex


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def tokens(redis: FakeRedis) -> TokenService:
    return TokenService(redis)  # type: ignore[arg-type]


def _refresh_jti(token: str) -> str:
    return str(decode_token(token, expected_type="refresh")["jti"])


# --------------------------------------------------------------------------
# Issuing
# --------------------------------------------------------------------------


def test_issue_pair_mints_two_different_tokens(tokens: TokenService) -> None:
    pair = tokens.issue_pair(uuid.uuid4())

    assert pair.access_token != pair.refresh_token
    assert decode_token(pair.access_token, expected_type="access")["typ"] == "access"
    assert decode_token(pair.refresh_token, expected_type="refresh")["typ"] == "refresh"


def test_both_tokens_name_the_same_subject(tokens: TokenService) -> None:
    user_id = uuid.uuid4()

    pair = tokens.issue_pair(user_id)

    access = decode_token(pair.access_token, expected_type="access")
    refresh = decode_token(pair.refresh_token, expected_type="refresh")
    assert access["sub"] == refresh["sub"] == str(user_id)


def test_the_two_lifetimes_come_from_settings(tokens: TokenService) -> None:
    """The asymmetry the whole design rests on: short access, long refresh."""
    settings = get_settings()
    pair = tokens.issue_pair(uuid.uuid4())

    access = decode_token(pair.access_token, expected_type="access")
    refresh = decode_token(pair.refresh_token, expected_type="refresh")
    access_ttl = access["exp"] - access["iat"]
    refresh_ttl = refresh["exp"] - refresh["iat"]

    assert access_ttl == settings.access_token_expire_minutes * 60
    assert refresh_ttl == settings.refresh_token_expire_days * 24 * 60 * 60
    assert access_ttl < refresh_ttl


# --------------------------------------------------------------------------
# Rotation
# --------------------------------------------------------------------------


async def test_rotation_revokes_the_presented_token(tokens: TokenService, redis: FakeRedis) -> None:
    pair = tokens.issue_pair(uuid.uuid4())
    old_jti = _refresh_jti(pair.refresh_token)

    await tokens.rotate(pair.refresh_token)

    assert await tokens.is_revoked(old_jti) is True
    assert f"{REVOKED_KEY_PREFIX}{old_jti}" in redis.values


async def test_rotation_returns_a_usable_new_pair(tokens: TokenService) -> None:
    pair = tokens.issue_pair(uuid.uuid4())

    rotated = await tokens.rotate(pair.refresh_token)

    assert rotated.refresh_token != pair.refresh_token
    assert await tokens.is_revoked(_refresh_jti(rotated.refresh_token)) is False


async def test_replaying_a_spent_token_is_refused(tokens: TokenService) -> None:
    """Single use. The reason a stolen refresh token is survivable."""
    pair = tokens.issue_pair(uuid.uuid4())
    await tokens.rotate(pair.refresh_token)

    with pytest.raises(AuthenticationError):
        await tokens.rotate(pair.refresh_token)


async def test_an_access_token_cannot_be_rotated(tokens: TokenService) -> None:
    pair = tokens.issue_pair(uuid.uuid4())

    with pytest.raises(AuthenticationError):
        await tokens.rotate(pair.access_token)


# --------------------------------------------------------------------------
# Revocation and its TTL
# --------------------------------------------------------------------------


async def test_the_denylist_entry_expires_with_the_token(
    tokens: TokenService, redis: FakeRedis
) -> None:
    """The property that bounds the denylist.

    Store a revocation for longer than its token lives and the key space grows
    forever; store it for less and a revoked token quietly works again. Matching
    the two keeps the denylist proportional to live sessions on its own.
    """
    settings = get_settings()
    pair = tokens.issue_pair(uuid.uuid4())

    await tokens.revoke(pair.refresh_token)

    ttl = redis.expiries[f"{REVOKED_KEY_PREFIX}{_refresh_jti(pair.refresh_token)}"]
    expected = settings.refresh_token_expire_days * 24 * 60 * 60
    assert expected - CLOCK_TOLERANCE_SECONDS <= ttl <= expected


async def test_revoking_rubbish_is_silent(tokens: TokenService, redis: FakeRedis) -> None:
    """Logout must be idempotent, and must not report which tokens are real."""
    await tokens.revoke("not-a-token")
    await tokens.revoke("")

    assert redis.values == {}


async def test_revoking_an_access_token_does_nothing(
    tokens: TokenService, redis: FakeRedis
) -> None:
    """Only refresh tokens are revocable; an access token is the wrong type."""
    pair = tokens.issue_pair(uuid.uuid4())

    await tokens.revoke(pair.access_token)

    assert redis.values == {}


async def test_a_token_expiring_mid_revocation_is_left_alone(
    tokens: TokenService, redis: FakeRedis
) -> None:
    """The race between `decode_token` accepting a token and the write happening.

    Vanishingly unlikely and entirely real: with a negative TTL, Redis would
    reject the write, so the branch returns instead. Reached here by handing the
    private method claims that have already expired — the only way to make a
    race deterministic.
    """
    expired_claims: dict[str, Any] = {
        "jti": "already-dead",
        "exp": (datetime.now(UTC) - timedelta(seconds=30)).timestamp(),
    }

    await tokens._revoke_claims(expired_claims)  # noqa: SLF001 — no public path to this branch

    assert redis.values == {}


def test_seconds_until_expiry_rounds_up() -> None:
    """Rounding down would leave a sub-second window in which a revoked token
    is valid again."""
    claims = {"exp": (datetime.now(UTC) + timedelta(seconds=10.2)).timestamp()}

    assert TokenService._seconds_until_expiry(claims) == 11  # noqa: SLF001
