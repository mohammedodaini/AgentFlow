# ruff: noqa: F401  — remove once this module is implemented (M2)
"""FastAPI dependency yielding one AsyncSession per request.

Layer: db. Session-per-request: opened when a route needs it, committed on
success, rolled back on exception, always closed. Routes/services receive it
via DI — nothing constructs its own session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory

# TODO(M2): async def get_db() -> AsyncIterator[AsyncSession] — yield/commit/rollback/close
