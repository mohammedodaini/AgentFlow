"""Shared pytest fixtures for the whole suite.

Only fixtures that more than one test module needs belong here. A fixture used
by a single file belongs in that file — a bloated conftest is how test suites
become impossible to reason about.

`_test_env` is autouse because `get_settings()` is `lru_cache`d: without
clearing that cache between tests, the first test to call it would freeze
configuration for every test that follows.

Isolation from your development data
------------------------------------
The autouse fixture also repoints `DATABASE_URL` at `<name>_test` and
`REDIS_URL` at database 1. M2's tests only read, so pointing at the dev stack
was harmless; M3's tests create users, organizations and revoked tokens. A test
run that quietly deletes the data you were looking at five minutes ago is a
test run nobody trusts afterwards.

The full apparatus — transactional rollback per test, factories, a coverage
gate — is M4. This is the smallest thing that keeps M3 honest.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.main import create_app
from app.models import Base

MAINTENANCE_DATABASE = "postgres"
"""Every PostgreSQL server has it, and you must be connected *somewhere* to
issue `CREATE DATABASE` — you cannot create a database from inside itself."""

TEST_REDIS_DB = 1
"""Development uses database 0. Tests flush their own, so they must not share."""

TRUNCATE_STATEMENT = text("TRUNCATE organizations, users, memberships RESTART IDENTITY CASCADE")
"""One catalogue operation instead of a per-row scan, and CASCADE follows the
foreign keys so the order the tables are listed in stops mattering."""


def resolve_test_database_url() -> str:
    """The configured database URL with a `_test` database name.

    CI already points `DATABASE_URL` at `agentflow_test`, so the suffix is only
    appended when missing — which keeps one rule ("tests use a database whose
    name ends in _test") true in both environments, and makes this function
    safe to call after it has already been applied.
    """
    url = make_url(Settings().database_url)
    name = url.database or "agentflow"

    if not name.endswith("_test"):
        name = f"{name}_test"

    return url.set(database=name).render_as_string(hide_password=False)


def resolve_test_redis_url() -> str:
    """The configured Redis URL pointed at the test database index."""
    return make_url(Settings().redis_url).set(database=str(TEST_REDIS_DB)).render_as_string()


async def _ensure_database_exists(url: str) -> None:
    """Create the test database if it is not there yet.

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
                # The name comes from our own settings, not user input, and
                # identifiers cannot be parameterised in DDL anyway.
                await connection.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        await admin_engine.dispose()


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force `APP_ENV=test`, isolate the datastores, and clear the settings cache."""
    database_url = resolve_test_database_url()
    redis_url = resolve_test_redis_url()

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("REDIS_URL", redis_url)

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """An engine on the test database, with the schema created and then emptied.

    Tables come from `Base.metadata.create_all`, not from running migrations.
    That is a real tradeoff: it is fast and keeps schema bugs separate from
    migration bugs, but it means these tests would still pass if a migration
    were missing. `alembic check` answers that question instead — it is the
    tool built for it.

    Skips rather than fails when Postgres is unreachable, so `pytest` on a
    laptop with Docker stopped reports skips instead of a wall of connection
    errors. CI always has a server, so nothing is quietly lost.

    `NullPool` because each test opens a couple of connections and then exits;
    pooling across a whole suite is how you reach "too many clients already".
    """
    url = resolve_test_database_url()

    try:
        await _ensure_database_exists(url)
    except (OSError, SQLAlchemyError) as exc:
        pytest.skip(f"PostgreSQL unavailable at {make_url(url).host}: {type(exc).__name__}")

    engine = create_async_engine(url, poolclass=NullPool)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as connection:
        await connection.execute(TRUNCATE_STATEMENT)

    await engine.dispose()


@pytest.fixture
def app() -> FastAPI:
    """A freshly built application.

    Built per-test through the factory rather than imported as a module-level
    singleton — that is the entire reason `create_app()` exists.
    """
    return create_app()


@pytest.fixture
async def client(app: FastAPI, db_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """HTTP client speaking to the ASGI app in-process (no network, no server).

    Depends on `db_engine` so the schema exists and is cleaned up afterwards —
    the app builds its *own* engine in `lifespan`, pointed at the same test
    database by the autouse env fixture.

    `lifespan_context` is entered explicitly because httpx's ASGITransport does
    not fire lifespan events. Without it, startup never runs, so
    `app.state.session_factory` never exists and any route touching the
    database fails with an AttributeError that looks nothing like the real
    problem. Entering it here also means tests exercise the same startup path
    production uses.
    """
    async with app.router.lifespan_context(app):
        # Redis test database 1, wiped so a previous run's revoked tokens
        # cannot make this run's fresh tokens look stolen.
        await app.state.redis.flushdb()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as async_client:
            yield async_client
