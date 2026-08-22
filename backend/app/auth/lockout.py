"""Locking an account after repeated failed sign-ins.

Layer: auth. The gap a production audit named: `EventType.USER_SIGN_IN_FAILED`
recorded every guess with its address and its email, and **nothing acted on it**.
A detection mechanism nobody reads is a log, not a defence.

What this is, and what the rate limiter already does
-----------------------------------------------------
`RateLimitMiddleware` bounds requests **per address**. It is the right tool
against one machine hammering the API, and it is the wrong tool against the
attack that matters here: credential stuffing sprays a leaked username/password
list from thousands of addresses, a handful of attempts each. Every one of them
sits comfortably inside a per-IP budget while a single account is guessed
hundreds of times.

So this counts per **account**, which is the axis the attacker cannot spread
across — they want *that* account, and every guess at it lands in the same
counter wherever it comes from.

Why a lock and not a slow-down
-------------------------------
The gentler alternative is exponential backoff. It is better UX and it is weaker:
an attacker with a list does not wait, they move on and come back, and a delay
that is not enforced across processes is not enforced at all.

The cost is real and has to be stated: **an attacker who knows an email address
can lock that user out** by guessing wrong fifteen times. That is a denial of
service against one person, and it is the accepted trade — the alternative is
leaving the account guessable. The window is deliberately short so the damage
expires on its own, and a *successful* sign-in clears the counter, so a user who
gets their password right at attempt three is never affected.

Redis, not Postgres
-------------------
The same argument `app/db/redis.py` makes for the token denylist: this is data
that may disappear. Losing it means an attacker gets their attempt budget back,
which is the cheapest possible failure — and self-expiring keys mean nothing
accumulates and no sweeper is needed.
"""

from __future__ import annotations

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = structlog.get_logger(__name__)

KEY_PREFIX = "auth:failures:"
"""Namespaced, because this Redis database also holds the M3 refresh-token
denylist, the M11 OAuth states and arq's queues."""

MAX_FAILURES = 15
"""Wrong answers before the account is locked.

High enough that a person mistyping a password, trying an old one, and then
pasting from a manager never reaches it. Low enough that fifteen guesses against
one account is a rounding error in any credential-stuffing run — the attacker
gets fifteen tries per fifteen minutes rather than thousands.
"""

WINDOW_SECONDS = 15 * 60
"""How long failures are remembered, and therefore how long a lock lasts.

Fifteen minutes rather than an hour or "until an admin clears it". A lock that
outlives the attack punishes the *user*, and there is no admin here to clear it
(see `docs/operations.md` — one operator, no on-call). Short and self-expiring
means the worst case for a legitimate user is a coffee.
"""


class LockoutGuard:
    """Counts failed sign-ins per account and refuses when there are too many.

    Keyed on the email as supplied, lowercased. Not on the user id, deliberately:
    the id is unknown for an address that does not exist, and an attacker probing
    a list of addresses must be counted the same whether or not each one is real.
    Doing otherwise would also make the lock a *user-enumeration oracle* — locking
    only real accounts tells the attacker which addresses exist.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def is_locked(self, email: str) -> bool:
        """Whether this account has spent its attempts.

        **Fails open**, for the reason ADR-0019 gives about the rate limiter:
        Redis is the optional dependency here, and a cache blip must not become
        "nobody can sign in". The cost of failing open is that an attacker gets
        unmetered guesses during an outage; the cost of failing closed is a total
        authentication outage every time Redis restarts.
        """
        try:
            failures = await self._redis.get(_key(email))
        except RedisError:
            logger.warning("lockout.unavailable", detail="redis unreachable; allowing the attempt")
            return False

        return failures is not None and int(failures) >= MAX_FAILURES

    async def record_failure(self, email: str) -> int:
        """Count one wrong answer. Returns the running total."""
        key = _key(email)

        try:
            # Pipelined, so the counter and its expiry cannot be separated by a
            # crash — a key with no TTL would hold a count that never resets and
            # lock that account out permanently.
            pipeline = self._redis.pipeline()
            pipeline.incr(key)
            # Reset on every failure rather than only on the first. That makes the
            # window slide: fifteen guesses spread over an hour, one every four
            # minutes, would otherwise never trip it.
            pipeline.expire(key, WINDOW_SECONDS)
            failures = int((await pipeline.execute())[0])
        except RedisError:
            logger.warning("lockout.unavailable", detail="redis unreachable; not counting")
            return 0

        if failures == MAX_FAILURES:
            # Logged once, on the transition. Logging every attempt past the
            # threshold turns one attack into a thousand identical lines.
            logger.warning("auth.account_locked", failures=failures)

        return failures

    async def clear(self, email: str) -> None:
        """Forget the failures for an account.

        Called on a *successful* sign-in, which is what keeps this invisible to
        anyone who simply mistyped: get it right and the count is gone.
        """
        try:
            await self._redis.delete(_key(email))
        except RedisError:
            # Not worth a warning. The key expires on its own, and the only
            # consequence is a user carrying stale failures for a few minutes.
            logger.debug("lockout.clear_failed")


def _key(email: str) -> str:
    return f"{KEY_PREFIX}{email.strip().lower()}"
