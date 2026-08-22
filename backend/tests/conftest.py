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
import pathlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.db.deps import get_db
from app.db.redis import create_redis_client
from app.main import create_app
from app.models import Base
from app.rag.embeddings import EmbeddingProvider, create_embedder
from app.storage import ObjectStorage, create_storage
from app.workers.queue import get_queue

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
    """Create the test database if absent, then build the schema in it *fresh*.

    `CREATE DATABASE` cannot run inside a transaction, hence AUTOCOMMIT on the
    admin connection.

    Tables come from `Base.metadata.create_all`, not from running migrations.
    That is a real tradeoff: it is fast and it keeps schema bugs separate from
    migration bugs, but it means these tests would still pass if a migration
    were missing. `alembic check` answers that question instead — it is the tool
    built for it.

    Why the schema is dropped first (M10)
    -------------------------------------
    `create_all` is `CREATE TABLE IF NOT EXISTS`. It builds tables that are
    absent and *never alters one that already exists* — so the day a milestone
    adds a column to an existing table, every developer whose test database
    survived the previous run silently gets the old shape, and the failure
    surfaces as an `UndefinedColumnError` from inside an unrelated test. M10
    added `agent_runs.conversation_id` and hit exactly that.

    `DROP SCHEMA ... CASCADE` rather than `metadata.drop_all()`, because
    `drop_all` drops tables and **not** the native enum types they use — the same
    fact that has now needed a hand-written line in four migrations (M2, M5, M9,
    M10). Dropping tables alone would leave `run_status` behind, and the next
    `create_all` would fail with "type run_status already exists".
    """
    target = make_url(url)

    # A guard, not a formality. This function drops everything in the database it
    # is handed, and it takes its URL from `DATABASE_URL` — one careless export
    # and the target is a real database. The `_test` suffix is the invariant
    # `resolve_test_database_url` exists to maintain; this refuses to proceed if
    # anything has broken it.
    if not (target.database or "").endswith("_test"):
        message = (
            f"Refusing to rebuild the schema in {target.database!r}: the test "
            "database name must end in '_test'."
        )
        raise RuntimeError(message)
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
            # Tables *and* types, in one statement. See the docstring.
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))

            # M6. `create_all` builds tables, not extensions, and a
            # `vector(1536)` column cannot be created before the type exists.
            # The migration does this too; the test schema is built from metadata
            # rather than migrations (ADR-0006), so it needs its own. It also has
            # to come *after* the schema is recreated — the extension lives in
            # `public` and went down with it.
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
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
def _no_outbound_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse any real outbound HTTP request from the test suite (M14).

    **A test reached slack.com and nobody noticed.** An M14 e2e test connected a
    Slack workspace and then called `/integrations/slack/channels`; the offline
    authorization server had issued a token no real Slack would honour, so the
    request went out over the wire, took 252ms, came back 401, and the assertion
    failed for a reason that had nothing to do with the code under test.

    That is the mild version. The dangerous one is the same request *succeeding* —
    a suite that quietly depends on a third party is green on a good day, red
    during somebody else's incident, and green again by the time anyone looks.
    It also makes the suite unrunnable on a plane, which is where a fair amount of
    this project was written.

    Every provider client takes a `transport` for exactly this reason
    (`BaseClient.__init__`), and the unit tests use `MockTransport`. This fixture
    makes forgetting it a loud, immediate failure naming the URL, rather than a
    slow test with a confusing result.

    Only `AsyncHTTPTransport` is blocked — the class that opens sockets.
    `MockTransport` and `ASGITransport` are different classes and keep working, so
    nothing that was already honest has to change. Postgres and Redis do not go
    through httpx at all.
    """

    async def refuse(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        del self
        message = (
            f"A test made a real network request to {request.url}. "
            "Pass transport=httpx.MockTransport(...) to the client under test, "
            "or use the offline provider."
        )
        raise AssertionError(message)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> Iterator[None]:
    """Force `APP_ENV=test`, isolate the datastores, clear the settings cache.

    Autouse because `get_settings()` is `lru_cache`d: without clearing that
    cache between tests, the first test to call it would freeze configuration
    for every test that follows.

    `STORAGE_LOCAL_PATH` joins the list at M5, for the same reason as the other
    two. The default is `./var/storage`, so without this every test that
    uploaded anything would scatter files through the working tree and leave
    them there — and two tests uploading the same filename would start seeing
    each other's bytes. `tmp_path` is function-scoped, so each test gets an
    empty directory and pytest cleans it up.
    """
    # **The suite reads no `.env` file at all**, and that is stronger than
    # clearing variables. `Settings.model_config` lists `../.env` and `.env` as
    # sources, and pydantic-settings parses those files *itself* — the values
    # never pass through `os.environ`, so `monkeypatch.delenv` cannot touch
    # them. Deleting `APP_VERSION` and watching `test_health.py` still fail with
    # `'tlsdrill' != 'dev'` is how that was established.
    #
    # `setitem` rather than assignment so pytest restores the class attribute
    # afterwards; `make dev` in the same working tree is unaffected.
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    # Resolved *before* the sweep below, because these two read the environment
    # to find out where CI put Postgres and Redis.
    database_url = resolve_test_database_url()
    redis_url = resolve_test_redis_url()

    # And no *exported* variable either — the same isolation from the other
    # direction, for a developer with `METRICS_TOKEN` in their shell.
    #
    # It has now gone wrong twice through the file. First `METRICS_TOKEN`, which
    # made `/metrics` demand a token and gave four mystery 401s in
    # `test_hardening.py`. The fix was one `delenv`, with a comment saying "the
    # next setting somebody puts in `.env` would break a different file" — and
    # the next setting was `APP_VERSION`, during a TLS drill, breaking
    # `test_health.py` with `'tlsdrill' != 'dev'`. This is the general form.
    #
    # Derived from `Settings` rather than written out, so a field added in a
    # later milestone is covered on the day it is added. Anything the suite
    # actually wants is set immediately below.
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias
        monkeypatch.delenv(
            str(alias) if isinstance(alias, str) else name.upper(), raising=False
        )

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path / "storage"))
    # M16. The limiter counts per caller per minute in a *shared* Redis, and the
    # suite runs hundreds of requests from one address inside one window — so the
    # hundredth test would fail for something the first test did, and which test
    # broke would depend on ordering. `tests/e2e/test_rate_limit.py` turns it back
    # on deliberately, which is the only place its behaviour is asserted.
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    # Explicit rather than merely absent: `/metrics` is open in the suite, and
    # `test_hardening.py` sets a token where it wants to assert the check.
    monkeypatch.setenv("METRICS_TOKEN", "")

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def storage() -> ObjectStorage:
    """The real local backend, rooted in this test's temporary directory.

    A real one rather than a mock. The interesting failures in `app/storage/`
    are filesystem failures — a key escaping its root, a directory that does
    not exist yet, a half-written file — and a mock reproduces none of them
    while happily reporting success.
    """
    return create_storage(get_settings())


@pytest.fixture
def embedder() -> EmbeddingProvider:
    """The offline embedder, built the way the application builds it.

    Real rather than mocked, for the same reason `storage` is: it produces
    genuine lexical similarity, so a retrieval test can assert that the *right*
    chunk ranked first instead of merely that some rows came back.
    """
    return create_embedder(get_settings())


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """A real Redis client on the test database, empty at the start of each test.

    Real rather than faked, for the same reason `storage` and `embedder` are. The
    behaviour M11 depends on is Redis' own: `SETEX` really expiring, and `GETDEL`
    being *atomic* — which is the whole reason a state cannot be consumed twice by
    two callbacks arriving together. A dict-shaped fake would satisfy every
    assertion while modelling neither.

    Flushed on entry rather than exit, so a test that fails mid-way leaves its keys
    behind for inspection instead of erasing the evidence.
    """
    client = create_redis_client(get_settings())
    await client.flushdb()

    try:
        yield client
    finally:
        await client.aclose()


class RecordingQueue:
    """A `JobQueue` that remembers instead of enqueueing.

    Substituted for the arq pool so tests can assert on the behaviour that
    matters — *that* a job was enqueued, with which arguments, and in what
    order relative to the commit — none of which is observable when the only
    evidence is a job sitting in Redis.

    It satisfies the `JobQueue` protocol structurally, which is the entire
    reason that protocol exists (see `app/workers/queue.py`).
    """

    def __init__(self, events: list[str]) -> None:
        self.jobs: list[dict[str, Any]] = []
        self._events = events

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> None:
        self._events.append("enqueue")
        self.jobs.append({"function": function, "args": args, **kwargs})


@pytest.fixture
def lifecycle_events() -> list[str]:
    """An ordered log of "commit" and "enqueue", shared by the fixtures below.

    This exists for one test, and that test earns it: the correctness of the
    whole 202 flow rests on the enqueue happening *after* the transaction
    commits, and that ordering is a promise made by FastAPI's dependency
    lifecycle rather than by any code in this repository. A comment claiming it
    is true would rot silently on a framework upgrade. A list that records the
    real order does not.
    """
    return []


@pytest.fixture
def queue(lifecycle_events: list[str]) -> RecordingQueue:
    return RecordingQueue(lifecycle_events)


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
def app(db_session: AsyncSession, queue: RecordingQueue, lifecycle_events: list[str]) -> FastAPI:
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

    # Record *every* commit, wherever it comes from — the route's explicit one
    # as well as the dependency's trailing one. Instrumenting only the teardown
    # was the first attempt and it measured the wrong thing: it could not see a
    # route that commits for itself, which is exactly what the upload endpoint
    # does and exactly what the ordering test is about.
    inner_commit = db_session.commit

    async def recording_commit() -> None:
        await inner_commit()
        lifecycle_events.append("commit")

    db_session.commit = recording_commit  # type: ignore[method-assign]

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

    # M5. The queue is overridden but storage is not: `lifespan()` already
    # builds a local backend rooted in this test's tmp_path (see `_test_env`),
    # so end-to-end tests exercise the real storage wiring. The queue has no
    # such harmless local equivalent — a real one would need arq to be running
    # and would make every upload test wait on a worker.
    application.dependency_overrides[get_queue] = lambda: queue

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
