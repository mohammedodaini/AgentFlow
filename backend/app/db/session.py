"""Async engine + session factory — the one place that knows how to connect.

Layer: db. Both functions are *builders*: they take their inputs as arguments
and return a new object, rather than creating a module-level singleton at
import time.

That mirrors why `create_app()` exists. A module-level engine is constructed
whenever the module is first imported, binding itself to whatever environment
happened to exist — which in tests is "whatever the previous test left behind".
Builders let `lifespan()` own the lifecycle explicitly: created on startup,
stored on `app.state`, disposed on shutdown.

Imported by: app/db/deps.py, app/main.py.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine and its connection pool.

    One engine per process. It is not a connection — it is a pool plus a
    dialect, and it is safe to share across every coroutine in the process.
    Creating a second one silently doubles your connection footprint.
    """
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # Verify a pooled connection is still alive before handing it out.
        # Load balancers, Postgres restarts and cloud idle timeouts all close
        # connections without telling the client; without pre-ping the app
        # discovers this by failing a user's request. The cost is one trivial
        # round trip per checkout — the cheapest insurance available here.
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the factory that produces one session per request.

    `expire_on_commit=False` is the setting that matters under asyncio. By
    default SQLAlchemy expires every attribute after a commit, so touching
    `user.email` afterwards triggers a lazy refresh — a hidden database call
    that, in async, raises `MissingGreenlet` from wherever you happened to be.
    Turning it off means a committed object stays usable, which is exactly what
    a route needs when it serialises the row it just wrote.
    """
    return async_sessionmaker(bind=engine, expire_on_commit=False)
