"""v1 aggregate router — main.py includes THIS, never individual route files.

Adding an endpoint = write the route module, register it here, done.

Route modules are imported here only once they exist; each milestone below
adds its own line as it lands.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    agent_runs,
    approvals,
    auth,
    conversations,
    documents,
    events,
    generation,
    health,
    integrations,
    memories,
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
api_router.include_router(agent_runs.router)
api_router.include_router(conversations.router)
api_router.include_router(memories.router)
api_router.include_router(integrations.router)
api_router.include_router(approvals.router)
api_router.include_router(events.router)

# Every milestone through M12 is now registered. M14 adds more integrations, and
# M16 adds the operational surfaces (metrics, rate limits).
