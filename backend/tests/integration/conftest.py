"""Fixtures for tests that talk to a real PostgreSQL server.

These are the tests unit tests cannot replace. A `UNIQUE` constraint, an
`ON DELETE CASCADE` and a server-side `now()` default are all enforced by the
database, so the only way to know they work is to ask the database.

Most of the machinery moved up to `tests/conftest.py` during M3, once the
end-to-end tests needed the same isolated test database that these do. What
remains here is the one fixture only this package uses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """One session per test.

    Cleanup lives in `db_engine` (it truncates on teardown), so a test that
    commits cannot leak rows into the next one.
    """
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)

    async with session_factory() as session:
        yield session
