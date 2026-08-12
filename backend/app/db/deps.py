"""FastAPI dependency yielding one AsyncSession per request.

Layer: db. Session-per-request: opened when a route needs it, committed on
success, rolled back on exception, always closed. Routes/services receive it
via DI — nothing constructs its own session.

Imported by: app/auth/dependencies.py and every route that touches data.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Read the factory that `lifespan()` stored on the application.

    `app.state` is untyped by design (it is a bare namespace), so this is the
    single place that narrows it back to a real type. Every other module then
    gets a properly typed factory instead of an `Any` that silently disables
    type checking for everything downstream.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a session scoped to exactly one HTTP request.

    This is the *unit of work* pattern: everything a request does succeeds
    together or fails together. A route that writes three tables and raises on
    the fourth leaves nothing behind.

    Why the commit lives here rather than inside each service: a service that
    commits can never be composed into a larger transaction — the moment two
    services must succeed atomically, every one of their commits has to be
    hunted down and removed. Owning the boundary at the edge keeps services
    composable. A service that needs a generated value mid-request calls
    `flush()`, which sends the INSERT without ending the transaction.

    The `async with` block closes the session and returns its connection to the
    pool on every path, including cancellation. Without it, one unhandled error
    leaks a connection, and enough of those exhaust the pool while the app
    still looks perfectly healthy.
    """
    session_factory = get_session_factory(request)

    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
