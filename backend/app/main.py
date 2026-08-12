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

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.db.session import create_engine, create_session_factory
from app.logging.config import configure_logging
from app.middleware.request_id import RequestIDMiddleware
from app.middleware.timing import TimingMiddleware

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

    # Note: nothing connects yet. SQLAlchemy pools lazily, so a database that
    # is down does not stop the process from starting — /health/ready is what
    # reports that, and an orchestrator can act on it.
    # TODO(M5): open the Redis connection pool

    yield

    # Close every pooled connection. Skipping this leaks server-side sessions
    # on every reload, and `make dev` reloads on each keystroke.
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

    app.include_router(api_router, prefix="/api/v1")

    return app


# Uvicorn target for `make dev` (see Makefile): app.main:app
app = create_app()
