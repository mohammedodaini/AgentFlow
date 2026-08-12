"""Readiness checks — can we actually serve? (DB reachable, Redis reachable.)

Liveness stays trivial in the route; THIS module owns dependency probes so
the route stays thin and the checks are unit-testable.

Imported by: app/api/v1/routes/health.py.

Why Redis is not probed yet
---------------------------
The original plan listed Redis alongside Postgres. Nothing uses Redis until
M5, and a readiness probe should report whether *this* deployment can serve
*its* traffic. Probing an unused dependency means a Redis blip pulls the API
out of the load balancer for no reason. The Redis check joins this module at
M5, together with the connection pool that makes it cheap.
"""

from __future__ import annotations

import asyncio

import structlog
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


async def check_readiness(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, bool]:
    """Run every dependency probe and report each one by name.

    A dict rather than a bare bool because "not ready" on its own tells an
    on-call engineer nothing. `{"database": false}` tells them where to look.

    Probes will run concurrently once there is more than one; wrapping a single
    call in `asyncio.gather` today would be ceremony.
    """
    return {"database": await check_database(session_factory)}
