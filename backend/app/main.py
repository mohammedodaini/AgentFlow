# ruff: noqa: F401  — remove once this module is implemented (M1)
"""Application entrypoint — the FastAPI app factory.

Layer: composition root. The ONLY place that wires everything together:
settings, logging, middleware, routers, lifespan (DB engine startup/shutdown).
Nothing imports from main.py; main.py imports from everywhere.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.logging.config import configure_logging

# TODO(M1): lifespan() — configure_logging(), create DB engine on startup, dispose on shutdown
# TODO(M1): create_app() -> FastAPI — app factory (NOT a module-level app; factories are testable)
# TODO(M1): app.include_router(api_router, prefix="/api/v1")
# TODO(M1): register middleware from app.middleware (request_id first, so all logs carry it)
