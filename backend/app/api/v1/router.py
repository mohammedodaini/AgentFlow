"""v1 aggregate router — main.py includes THIS, never individual route files.

Adding an endpoint = write the route module, register it here, done.

Route modules are imported here only once they exist; each milestone below
adds its own line as it lands.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    auth,
    documents,
    generation,
    health,
    organizations,
    retrieval,
    users,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(organizations.router)
api_router.include_router(documents.router)
api_router.include_router(retrieval.router)
api_router.include_router(generation.router)

# TODO(M9): agent_runs
# TODO(M10): conversations · TODO(M11): integrations · TODO(M12): approvals
