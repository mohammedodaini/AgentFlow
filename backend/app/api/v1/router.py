"""v1 aggregate router — main.py includes THIS, never individual route files.

Adding an endpoint = write the route module, register it here, done.

Route modules are imported here only once they exist; each milestone below
adds its own line as it lands.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import auth, health, organizations, users

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(organizations.router)

# TODO(M5): include documents · TODO(M6/M7): retrieval · TODO(M9): agent_runs
# TODO(M10): conversations · TODO(M11): integrations · TODO(M12): approvals
