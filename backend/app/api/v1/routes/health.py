"""GET /health — liveness (process up) and readiness (DB/Redis reachable).

The M1 milestone endpoint. Readiness logic lives in app/monitoring/health.py;
this file only exposes it over HTTP (routes stay thin).

Liveness vs readiness is not pedantry: an orchestrator *restarts* a container
that fails liveness, but only stops *routing traffic* to one that fails
readiness. Conflating them means a brief database blip restarts every pod.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class LivenessResponse(BaseModel):
    """Deliberately local rather than in schemas/: nothing else shares it."""

    status: Literal["ok"] = "ok"


@router.get("/health/live", summary="Liveness probe")
async def health_live() -> LivenessResponse:
    """Report that the process is alive.

    Touches no dependency on purpose. If this checked the database, a database
    outage would look like a dead application and get the container killed.
    """
    return LivenessResponse()


# TODO(M2): GET /health/ready — calls check_readiness(); 503 if a dependency is down
