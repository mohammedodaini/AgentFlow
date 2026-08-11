# ruff: noqa: F401  — remove once this module is implemented (M1)
"""v1 aggregate router — main.py includes THIS, never individual route files.

Adding an endpoint = write the route module, register it here, done.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    agent_runs,
    approvals,
    auth,
    conversations,
    documents,
    health,
    integrations,
    organizations,
    retrieval,
    users,
)

# TODO(M1): api_router = APIRouter(); include health
# TODO(M3): include auth, users, organizations
# TODO(M5): include documents · TODO(M6/M7): retrieval · TODO(M9): agent_runs
# TODO(M10): conversations · TODO(M11): integrations · TODO(M12): approvals
