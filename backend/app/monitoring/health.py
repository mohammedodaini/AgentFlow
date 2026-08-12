"""Readiness checks — can we actually serve? (DB reachable, Redis reachable.)

Liveness stays trivial in the route; THIS module owns dependency probes so
the route stays thin and the checks are unit-testable.

Imported by: app/api/v1/routes/health.py.

What gets probed, and when it started
-------------------------------------
Postgres from M2. Redis from M3 — not M5 as originally planned, because the
refresh-token denylist moved forward with `/auth/logout`, and the rule this
module follows is *probe what you actually use*. An unused dependency in the
readiness check means an outage in something irrelevant pulls a healthy API
out of the load balancer; a used one left out means the opposite, an instance
accepting traffic it cannot serve.
"""

from __future__ import annotations

import asyncio

import structlog
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

PROBE_TIMEOUT_SECONDS = 2.0
"""A hung check is worse than a failed one.

Without a timeout, an unreachable database leaves the probe waiting on a TCP
connect. The orchestrator's own probe then expires with no answer at all, so
every diagnosis reads "readiness timed out" instead of "the database is down".
Two seconds is comfortably longer than a healthy `SELECT 1` and far shorter
than any sensible probe deadline.
"""


async def check_database(session_factory: async_sessionmaker[AsyncSession]) -> bool:
    """Return whether Postgres answers a trivial query in time.

    `SELECT 1` deliberately touches no table. The question is "is the
    connection usable?", not "is the schema correct?" — a probe that queried a
    real table would start failing during an unrelated migration.
    """
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS), session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — a probe converts *any* failure to a bool
        # Letting an exception escape would turn a readiness check into a 500,
        # which is the one outcome an orchestrator cannot act on. The log line
        # preserves the detail that the boolean throws away.
        logger.warning(
            "health.database_unreachable",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False

    return True


async def check_redis(redis: Redis) -> bool:
    """Return whether Redis answers a PING in time.

    Readiness, not liveness: with Redis down, login still works but logout
    cannot revoke anything, so the instance should stop taking traffic rather
    than silently degrade to a system where signing out does nothing.
    """
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            await redis.ping()
    except Exception as exc:  # noqa: BLE001 — a probe converts *any* failure to a bool
        logger.warning(
            "health.redis_unreachable",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False

    return True


async def check_readiness(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis
) -> dict[str, bool]:
    """Run every dependency probe and report each one by name.

    A dict rather than a bare bool because "not ready" on its own tells an
    on-call engineer nothing. `{"database": false, "redis": true}` tells them
    where to look.

    The probes run concurrently. Sequentially, two 2-second timeouts stack into
    a 4-second worst case, and each dependency added later would push the probe
    further past the orchestrator's own deadline — at which point the readiness
    check fails because it is slow rather than because anything is wrong.
    """
    database_ok, redis_ok = await asyncio.gather(
        check_database(session_factory),
        check_redis(redis),
    )
    return {"database": database_ok, "redis": redis_ok}
