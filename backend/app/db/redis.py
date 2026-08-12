"""Redis client lifecycle — the second stateful dependency.

Layer: db. Same shape as session.py: a builder called by `lifespan()`, a
client stored on `app.state`, and a typed accessor for routes. Nothing here
creates a connection at import time.

Redis holds things that are allowed to disappear: the refresh-token denylist
(M3), the arq job queue (M5), rate-limit counters (M16). Postgres holds
everything that is not allowed to disappear. When it is unclear which one a
piece of data belongs in, ask what breaks if the cache is flushed — if the
answer is "a user has to log in again", Redis is right; if it is "we lost a
customer's document", it is not.

Arrived in M3 rather than M5 (the original plan) because `POST /auth/logout`
has to actually revoke something. A logout endpoint that returns 204 and
leaves the token working is worse than having no logout at all: it tells the
user they are safe when they are not.
"""

from __future__ import annotations

from fastapi import Request
from redis.asyncio import Redis

from app.core.config import Settings


def create_redis_client(settings: Settings) -> Redis:
    """Build the client and its connection pool.

    One per process, shared across coroutines — `redis.asyncio.Redis` is a pool
    handle, not a single connection.

    `decode_responses=True` returns `str` rather than `bytes`. Everything this
    application stores in Redis is text (jtis, job payloads, counters), and
    without it every read site would need its own `.decode()` — exactly the
    kind of detail that gets forgotten in one place out of ten.
    """
    # redis-py's `from_url` is untyped, so the annotation is what keeps this
    # module's callers from silently receiving `Any`.
    client: Redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        # Without a timeout an unreachable Redis makes the *request* hang
        # rather than fail — and a hung request holds its worker slot, so a
        # slow dependency becomes a full outage. Fail fast, surface it.
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    return client


def get_redis(request: Request) -> Redis:
    """Read the client `lifespan()` stored on the application.

    `app.state` is an untyped namespace, so this is the one place that narrows
    it back to a real type — the same job `get_session_factory` does for the
    database.
    """
    client: Redis = request.app.state.redis
    return client
