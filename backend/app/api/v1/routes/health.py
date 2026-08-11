# ruff: noqa: F401  — remove once this module is implemented (M1)
"""GET /health — liveness (process up) and readiness (DB/Redis reachable).

The M1 milestone endpoint. Readiness logic lives in app/monitoring/health.py;
this file only exposes it over HTTP (routes stay thin).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.monitoring.health import check_readiness

router = APIRouter(tags=["health"])

# TODO(M1): GET /health/live — static 200 {"status": "ok"}
# TODO(M2): GET /health/ready — calls check_readiness(); 503 if a dependency is down
