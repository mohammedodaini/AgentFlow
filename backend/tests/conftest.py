"""Shared pytest fixtures for the whole suite.

Only fixtures that more than one test module needs belong here. A fixture used
by a single file belongs in that file — a bloated conftest is how test suites
become impossible to reason about.

Three properties this file is responsible for
---------------------------------------------

**Isolation from your development data.** The autouse fixture repoints
`DATABASE_URL` at `<name>_test` and `REDIS_URL` at database 1. A test run that
quietly deletes the data you were looking at five minutes ago is a test run
nobody trusts afterwards.

**Isolation between tests.** Each test runs inside a transaction that is rolled
back when it finishes — including the work its HTTP requests did, because
`get_db` is overridden to hand routes the very same session. Nothing a test
writes can be seen by the next one, whatever order they run in.

**Speed.** The schema is created once per session, not once per test. Rolling
back a transaction costs microseconds; `TRUNCATE` costs a round trip per table
per test, and `create_all` costs far more than that.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.db.deps import get_db
from app.main import create_app
from app.models import Base

MAINTENANCE_DATABASE = "postgres"
"""Every PostgreSQL server has it, and you must be connected *somewhere* to
issue `CREATE DATABASE` — you cannot create a database from inside itself."""

TEST_REDIS_DB = 1
"""Development uses database 0. Tests flush their own, so they must not share."""


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


async def _create_schema(url: str) -> None:
    """Create the test database if absent, then build the schema in it.

    `CREATE DATABASE` cannot run inside a transaction, hence AUTOCOMMIT on the
    admin connection.

    Tables come from `Base.metadata.create_all`, not from running migrations.
    That is a real tradeoff: it is fast and it keeps schema bugs separate from
    migration bugs, but it means these tests would still pass if a migration
    were missing. `alembic check` answers that question instead — it is the
    tool built for it.
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

    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    """Prepare the test database once, and hand back its URL.

    Session-scoped and *synchronous*: it runs `asyncio.run` before any test's
    event loop exists, which sidesteps the whole question of what loop a
    session-scoped async fixture belongs to.

    Skips rather than fails when Postgres is unreachable, so `pytest` on a
    laptop with Docker stopped reports skips instead of a wall of connection
    errors. CI always has a server, so nothing is quietly lost.
    """
    url = resolve_test_database_url()

    try:
        asyncio.run(_create_schema(url))
    except (OSError, SQLAlchemyError) as exc:
        pytest.skip(f"PostgreSQL unavailable at {make_url(url).host}: {type(exc).__name__}")

    return url


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force `APP_ENV=test`, isolate the datastores, clear the settings cache.

    Autouse because `get_settings()` is `lru_cache`d: without clearing that
    cache between tests, the first test to call it would freeze configuration
    for every test that follows.
    """
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", resolve_test_database_url())
    monkeypatch.setenv("REDIS_URL", resolve_test_redis_url())

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def db_connection(database_url: str) -> AsyncIterator[AsyncConnection]:
    """One connection per test, wrapped in a transaction that is always undone.

    This is the isolation mechanism. Everything the test does — directly, or
    through an HTTP request — happens inside this transaction, and the rollback
    at the end erases all of it. No cleanup code, no ordering assumptions, and
    nothing left behind when a test fails halfway through.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield connection
        finally:
            await transaction.rollback()

    await engine.dispose()


@pytest.fixture
async def db_session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """A session bound to the test's transaction.

    `join_transaction_mode="create_savepoint"` is what makes this work with
    code that commits. Application code *should* commit — that is what it does
    in production — but a real COMMIT here would end the outer transaction and
    make the rollback a no-op, leaking rows into every later test. With this
    mode, `session.commit()` releases a SAVEPOINT instead, so the application
    behaves exactly as it does in production while the outer transaction stays
    open and rolls everything back at the end.
    """
    session = AsyncSession(
        bind=db_connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )

    try:
        yield session
    finally:
        await session.close()


@pytest.fixture
def app(db_session: AsyncSession) -> FastAPI:
    """A freshly built application, wired to the test's transaction.

    Built per-test through the factory rather than imported as a module-level
    singleton — that is the entire reason `create_app()` exists.

    The `get_db` override is the piece that makes end-to-end tests isolated:
    without it the app opens its own connections, commits for real, and leaves
    rows behind that the outer rollback never sees. With it, an HTTP request
    and the test that made it share one transaction — so a test can assert
    against the database directly and see exactly what the request wrote.
    """
    application = create_app()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        # Mirrors app/db/deps.py: commit on success, roll back on failure.
        # Both act on savepoints here rather than the outer transaction.
        try:
            yield db_session
        except Exception:
            await db_session.rollback()
            raise
        else:
            await db_session.commit()

    application.dependency_overrides[get_db] = override_get_db
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """HTTP client speaking to the ASGI app in-process (no network, no server).

    `lifespan_context` is entered explicitly because httpx's ASGITransport does
    not fire lifespan events. Without it, startup never runs, so
    `app.state.redis` never exists and any route touching Redis fails with an
    AttributeError that looks nothing like the real problem. Entering it here
    also means tests exercise the same startup path production uses.
    """
    async with app.router.lifespan_context(app):
        # Redis has no transactions to roll back, so it is flushed up front:
        # a previous run's revoked tokens must not make this run's fresh ones
        # look stolen.
        await app.state.redis.flushdb()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as async_client:
            yield async_client


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tag every test with the layer it lives in.

    Derived from the directory rather than written by hand, because a marker
    you have to remember is a marker that ends up on two thirds of the suite.
    Lets CI run `-m "not e2e"` for a fast signal on every push, and the whole
    pyramid before a merge.
    """
    for item in items:
        for layer in ("unit", "integration", "e2e"):
            if f"/tests/{layer}/" in item.nodeid or item.nodeid.startswith(f"tests/{layer}/"):
                item.add_marker(layer)
                break
