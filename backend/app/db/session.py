# ruff: noqa: F401  — remove once this module is implemented (M2)
"""Async engine + session factory — the one place that knows how to connect.

Layer: db. Engine is created once at app startup (main.py lifespan);
sessions are created per request via app/db/deps.py.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

# TODO(M2): create_engine() -> AsyncEngine — from settings.database_url, pool sizing
# TODO(M2): async_session_factory: async_sessionmaker[AsyncSession] — expire_on_commit=False
