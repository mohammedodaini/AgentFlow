"""Locking an account after repeated failed sign-ins (post-M16 audit).

The gap this closes: `EventType.USER_SIGN_IN_FAILED` recorded every guess with
its address and email, and nothing acted on it. A detection mechanism nobody
reads is a log, not a defence.

The distinction that matters is in `test_the_rate_limiter_does_not_cover_this`:
the limiter counts per *address*, and credential stuffing sprays one account from
thousands of addresses. This counts per *account*, which is the axis an attacker
cannot spread across.
"""

from __future__ import annotations

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.lockout import MAX_FAILURES, LockoutGuard
from app.auth.service import AuthService
from app.auth.tokens import TokenService
from app.core.exceptions import AuthenticationError
from app.models.event import Event, EventType
from app.schemas.auth import LoginRequest
from tests.factories import make_user

PASSWORD = "correct-horse-battery-staple"
EMAIL = "locked@agentflow.dev"


def auth(db_session: AsyncSession, redis_client: Redis, *, ip: str = "203.0.113.7") -> AuthService:
    return AuthService(
        db_session, TokenService(redis_client), ip_address=ip, lockout=LockoutGuard(redis_client)
    )


WRONG = "not-the-password"  # noqa: S105 — synthetic


async def guess(service: AuthService, *, email: str = EMAIL, password: str = WRONG) -> None:
    with pytest.raises(AuthenticationError):
        await service.login(LoginRequest(email=email, password=password))


async def test_an_account_locks_after_enough_wrong_answers(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """The feature, in one test."""
    await make_user(db_session, email=EMAIL, password=PASSWORD)
    service = auth(db_session, redis_client)

    for _ in range(MAX_FAILURES):
        await guess(service)

    # The *right* password now, and it is still refused.
    with pytest.raises(AuthenticationError):
        await service.login(LoginRequest(email=EMAIL, password=PASSWORD))


async def test_the_correct_password_still_works_below_the_threshold(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Somebody who mistypes twice and then pastes from a password manager must
    never learn this feature exists."""
    user = await make_user(db_session, email=EMAIL, password=PASSWORD)
    service = auth(db_session, redis_client)

    for _ in range(MAX_FAILURES - 1):
        await guess(service)

    signed_in, _ = await service.login(LoginRequest(email=EMAIL, password=PASSWORD))

    assert signed_in.id == user.id


async def test_signing_in_forgives_earlier_failures(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Getting it right clears the count, so a slow-typing week never accumulates
    into a lockout."""
    await make_user(db_session, email=EMAIL, password=PASSWORD)
    service = auth(db_session, redis_client)

    for _ in range(MAX_FAILURES - 1):
        await guess(service)
    await service.login(LoginRequest(email=EMAIL, password=PASSWORD))

    # A fresh budget: another near-miss run still ends in a successful sign-in.
    for _ in range(MAX_FAILURES - 1):
        await guess(service)
    signed_in, _ = await service.login(LoginRequest(email=EMAIL, password=PASSWORD))

    assert signed_in.email == EMAIL


async def test_an_address_that_does_not_exist_is_counted_too(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """**The enumeration oracle this avoids.**

    Counting only real accounts would mean the lock itself answers "does this
    address exist?" — the attacker sprays a list, sees which addresses start
    refusing early, and has enumerated your users. So every rejection counts,
    real or not.
    """
    service = auth(db_session, redis_client)
    ghost = "nobody-here@agentflow.dev"

    for _ in range(MAX_FAILURES):
        await guess(service, email=ghost)

    guard = LockoutGuard(redis_client)
    assert await guard.is_locked(ghost) is True


async def test_a_locked_account_says_nothing_different(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """ "This account is locked" would confirm the address exists and tell an
    attacker their spray is working. Same message as every other rejection — the
    identical argument that makes the unknown-email path hash a dummy value."""
    await make_user(db_session, email=EMAIL, password=PASSWORD)
    service = auth(db_session, redis_client)

    with pytest.raises(AuthenticationError) as first:
        await service.login(LoginRequest(email=EMAIL, password="wrong"))

    for _ in range(MAX_FAILURES):
        await guess(service)

    with pytest.raises(AuthenticationError) as locked:
        await service.login(LoginRequest(email=EMAIL, password=PASSWORD))

    assert str(locked.value) == str(first.value)


async def test_the_rate_limiter_does_not_cover_this(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """**Why this exists at all, given there is already a rate limiter.**

    `RateLimitMiddleware` counts per *address*. Credential stuffing sprays one
    account from thousands of addresses, a handful of attempts each — every one
    comfortably inside a per-IP budget. Here each attempt arrives from a different
    address and the account still locks, because the count follows the account.
    """
    await make_user(db_session, email=EMAIL, password=PASSWORD)

    for index in range(MAX_FAILURES):
        # A different source address every time, as a botnet would have.
        await guess(auth(db_session, redis_client, ip=f"203.0.113.{index}"))

    with pytest.raises(AuthenticationError):
        await auth(db_session, redis_client, ip="198.51.100.1").login(
            LoginRequest(email=EMAIL, password=PASSWORD)
        )


async def test_locking_is_recorded_in_the_audit_trail(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """A refused-because-locked attempt is still a security event, and it has its
    own reason so "how long has this been going on?" is answerable."""
    await make_user(db_session, email=EMAIL, password=PASSWORD)
    service = auth(db_session, redis_client)

    for _ in range(MAX_FAILURES + 1):
        await guess(service)

    events = list(
        await db_session.scalars(
            select(Event).where(Event.event_type == EventType.USER_SIGN_IN_FAILED.value)
        )
    )
    reasons = {event.payload.get("reason") for event in events}

    assert "locked" in reasons


async def test_it_fails_open_when_redis_is_gone(
    db_session: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0019's rule, applied again: Redis is the optional dependency, and a
    cache blip must not become "nobody can sign in"."""
    await make_user(db_session, email=EMAIL, password=PASSWORD)

    def explode(*args: object, **kwargs: object) -> None:
        raise RedisConnectionError("redis is down")

    monkeypatch.setattr(redis_client, "get", explode)
    monkeypatch.setattr(redis_client, "pipeline", explode)

    signed_in, _ = await auth(db_session, redis_client).login(
        LoginRequest(email=EMAIL, password=PASSWORD)
    )

    assert signed_in.email == EMAIL
