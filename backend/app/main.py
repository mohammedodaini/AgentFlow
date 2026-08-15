"""Application entrypoint — the FastAPI app factory.

Layer: composition root. The ONLY place that wires everything together:
settings, logging, middleware, routers, lifespan (DB engine startup/shutdown).
Nothing imports from main.py; main.py imports from everywhere.

Why a factory instead of a module-level `app = FastAPI()`: a module-level app
is built at import time with whatever environment happened to exist, and every
test then shares one mutated instance. `create_app()` gives each test a clean
application, and makes "build an app configured differently" a function call
rather than a monkeypatch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api.errors import register_exception_handlers
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.db.redis import create_redis_client
from app.db.session import create_engine, create_session_factory
from app.integrations import create_oauth_registry
from app.llm import create_llm
from app.logging.config import configure_logging
from app.middleware.request_id import RequestIDMiddleware
from app.middleware.timing import TimingMiddleware
from app.rag.embeddings import create_embedder
from app.storage import create_storage
from app.workers.queue import create_queue

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hooks.

    Everything expensive and long-lived belongs here rather than at import
    time: the DB engine (M2) and Redis pool (M5) are created on startup and
    disposed on shutdown, so connections are never leaked between reloads.
    """
    settings = get_settings()
    logger.info("app.startup", app_name=settings.app_name, env=settings.env)

    # The engine is created here, not at import time, so building an app never
    # has the side effect of opening a connection pool. It lands on app.state
    # because that is the object every request already holds a handle to —
    # which is what lets `get_db()` stay a plain function instead of a global.
    engine = create_engine(settings)
    app.state.db_engine = engine
    app.state.session_factory = create_session_factory(engine)

    # M3: the refresh-token denylist lives here. Arrived earlier than planned
    # because `/auth/logout` has to revoke something real.
    app.state.redis = create_redis_client(settings)

    # M5: where uploaded document bytes go. Built here for the same reason as
    # the engine — constructing an app should have no side effects, and a test
    # can point it at a temporary directory by overriding one dependency.
    app.state.storage = create_storage(settings)

    # M5: the producer side of the arq queue. A *second* Redis pool, and
    # deliberately not `app.state.redis` — that client decodes replies to str,
    # while arq's job payloads are bytes. See app/workers/queue.py.
    app.state.queue = await create_queue(settings)

    # M11: one OAuth provider instance per process, not per request. The offline
    # provider *is* an authorization server and holds the codes it has issued —
    # rebuilt per request, a code minted by /connect would be unknown to
    # /callback and every connect flow would fail. See app/integrations/.
    app.state.oauth_registry = create_oauth_registry(settings)

    # M6: the embedding provider. Built once per process because it owns an
    # HTTP client and its connection pool — constructing one per search would
    # open and tear down a TLS connection on every query.
    app.state.embedder = create_embedder(settings)

    # M7: the generation model, built once for the same reason as the embedder.
    # `AsyncAnthropic` owns a connection pool, and a pool per question would
    # spend a TLS handshake on every answer.
    app.state.llm = create_llm(settings)

    # Note: apart from the queue, nothing connects yet. The other clients pool
    # lazily, so a dependency that is down does not stop the process from
    # starting — /health/ready is what reports that, and an orchestrator can
    # act on it.

    yield

    # Close every pooled connection. Skipping this leaks server-side sessions
    # on every reload, and `make dev` reloads on each keystroke.
    await app.state.queue.aclose()
    await app.state.redis.aclose()
    await engine.dispose()
    logger.info("app.shutdown")


def create_app() -> FastAPI:
    """Build and wire a fully configured application."""
    settings = get_settings()

    # Before anything else, so even startup logs are structured.
    configure_logging()

    app = FastAPI(
        title="AgentFlow AI",
        version="0.1.0",
        debug=settings.debug,
        lifespan=lifespan,
    )

    # Middleware order matters. Starlette wraps outward, so the LAST one
    # registered is the OUTERMOST. RequestID must be outermost: it sets the
    # contextvar that TimingMiddleware's log line depends on.
    app.add_middleware(TimingMiddleware)
    app.add_middleware(RequestIDMiddleware)

    # Maps the domain exception hierarchy (app/core/exceptions.py) onto HTTP
    # status codes and one error body shape. Registered here rather than
    # per-route so a service raising NotFoundError becomes a 404 everywhere,
    # including in routes nobody has written yet.
    register_exception_handlers(app)

    app.include_router(api_router, prefix="/api/v1")

    return app


# Uvicorn target for `make dev` (see Makefile): app.main:app
app = create_app()
