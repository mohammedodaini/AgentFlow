"""Fixtures for tests that talk to a real PostgreSQL server (M2).

These are the tests unit tests cannot replace. A `UNIQUE` constraint, an
`ON DELETE CASCADE` and a server-side `now()` default are all enforced by the
database, so the only way to know they work is to ask the database.

The full testing apparatus — transactional isolation, factories, a coverage
gate — is M4. This file is the minimum that lets M2 prove its schema, and it is
deliberately small enough to be replaced rather than extended.

Two choices worth knowing:

* **A separate `_test` database.** Tests create and truncate tables. Pointing
  them at the development database means one `pytest` run silently deletes the
  data you were looking at five minutes ago.
* **Skip, don't fail, when Postgres is absent.** `pytest` on a laptop with
  Docker stopped should report "skipped", not a wall of connection errors that
  buries the real failures. CI always has a server, so nothing is quietly lost.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models import Base

MAINTENANCE_DATABASE = "postgres"
"""Every PostgreSQL server has it, and you must be connected *somewhere* to
issue `CREATE DATABASE` — you cannot create a database from inside itself."""


def resolve_test_database_url() -> str:
    """Return the configured URL with a `_test` database name.

    CI already points `DATABASE_URL` at `agentflow_test`, so the suffix is only
    appended when missing. That keeps one rule — "tests use a database whose
    name ends in _test" — true in both environments.

    (Not named `test_*`: pytest would collect it as a test case.)
    """
    url = make_url(get_settings().database_url)
    name = url.database or "agentflow"

    if not name.endswith("_test"):
        name = f"{name}_test"

    return url.set(database=name).render_as_string(hide_password=False)


async def _ensure_database_exists(url: str) -> None:
    """Create the test database if it does not exist yet.

    `CREATE DATABASE` cannot run inside a transaction, hence AUTOCOMMIT.
    """
    target = make_url(url)
    admin_engine = create_async_engine(
        target.set(database=MAINTENANCE_DATABASE).render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )

    try:
        async with admin_engine.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            )
            if not exists:
                # The name comes from our own settings, not from user input,
                # and identifiers cannot be parameterised in DDL anyway.
                await connection.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        await admin_engine.dispose()


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """An engine bound to the test database, with the schema already created.

    Tables come from `Base.metadata.create_all`, not from running migrations.
    That is a real tradeoff: it is fast and it keeps schema bugs separate from
    migration bugs, but it means these tests would still pass if a migration
    were missing. `alembic check` answers that question instead — it is the
    tool built for it, and it runs against the models directly.

    `NullPool` because each test opens a couple of connections and then exits;
    pooling across a whole suite is how you reach "too many clients already".
    """
    url = resolve_test_database_url()

    try:
        await _ensure_database_exists(url)
    except (OSError, SQLAlchemyError) as exc:  # server down, wrong port, no Docker
        pytest.skip(f"PostgreSQL unavailable at {make_url(url).host}: {type(exc).__name__}")

    engine = create_async_engine(url, poolclass=NullPool)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """One session per test, with every table emptied afterwards.

    TRUNCATE rather than DELETE: a single catalogue operation instead of a
    per-row scan, and `CASCADE` follows the foreign keys so the order the
    tables are listed in stops mattering.
    """
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)

    async with session_factory() as session:
        yield session

    async with db_engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE organizations, users, memberships RESTART IDENTITY CASCADE")
        )
