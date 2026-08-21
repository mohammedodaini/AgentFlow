"""GET /health — liveness (process up) and readiness (DB/Redis reachable).

The M1 milestone endpoint. Readiness logic lives in app/monitoring/health.py;
this file only exposes it over HTTP (routes stay thin).

Liveness vs readiness is not pedantry: an orchestrator *restarts* a container
that fails liveness, but only stops *routing traffic* to one that fails
readiness. Conflating them means a brief database blip restarts every pod.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.db.deps import get_session_factory
from app.db.redis import get_redis
from app.monitoring.health import check_readiness

router = APIRouter(tags=["health"])


class LivenessResponse(BaseModel):
    """Deliberately local rather than in schemas/: nothing else shares it."""

    status: Literal["ok"] = "ok"
    version: str = "dev"
    """Which build answered. Unauthenticated, and that is a considered choice:
    a git SHA tells an attacker which commit is deployed, and this repository is
    private. Weigh it again if the source is ever public — the cost is that
    verifying a deploy or a rollback stops being a curl."""


@router.get("/health/live", summary="Liveness probe")
async def health_live(settings: Annotated[Settings, Depends(get_settings)]) -> LivenessResponse:
    """Report that the process is alive, and which build it is.

    Touches no dependency on purpose. If this checked the database, a database
    outage would look like a dead application and get the container killed.

    The version is here rather than on `/health/ready` because this is the probe
    that always answers. During a bad deploy readiness is exactly what is failing,
    and that is precisely when somebody needs to know which build is running.
    """
    return LivenessResponse(version=settings.app_version)


class ReadinessResponse(BaseModel):
    """The probe result, plus a per-dependency breakdown for whoever is paged."""

    status: Literal["ready", "not_ready"]
    checks: dict[str, bool]


@router.get(
    "/health/ready",
    summary="Readiness probe",
    responses={HTTPStatus.SERVICE_UNAVAILABLE: {"description": "A dependency is unreachable"}},
)
async def health_ready(request: Request, response: Response) -> ReadinessResponse:
    """Report whether this instance can serve traffic right now.

    Returns 503 when any dependency fails, which is what tells a load balancer
    to route around this instance without restarting it.

    The body is returned on both paths rather than raising `HTTPException`,
    because the interesting information is *which* dependency failed — and an
    error response that says only "Service Unavailable" sends whoever is
    on-call straight to the logs to find out what a 503 meant.
    """
    checks = await check_readiness(get_session_factory(request), get_redis(request))
    ready = all(checks.values())

    if not ready:
        response.status_code = HTTPStatus.SERVICE_UNAVAILABLE

    return ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)
