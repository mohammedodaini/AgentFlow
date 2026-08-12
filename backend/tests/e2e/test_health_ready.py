"""GET /api/v1/health/ready over HTTP (M2).

The probe function has its own unit tests. What is verified here is the part
only the full stack can show: that the route reaches the session factory
lifespan put on `app.state`, and that a failing dependency becomes a 503
rather than a 200 carrying sad-looking JSON.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from httpx import AsyncClient

from app.api.v1.routes import health

READY_URL = "/api/v1/health/ready"


async def test_returns_200_when_the_database_is_reachable(client: AsyncClient) -> None:
    """End to end against the real Postgres from `make up`."""
    response = await client.get(READY_URL)

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ready", "checks": {"database": True, "redis": True}}


async def test_returns_503_when_a_dependency_is_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """503 is what tells a load balancer to route around this instance.

    A 200 carrying `{"database": false}` would leave the instance in rotation
    serving errors, because nothing upstream reads the body.
    """

    async def _database_down(_session_factory: object, _redis: object) -> dict[str, bool]:
        return {"database": False, "redis": True}

    monkeypatch.setattr(health, "check_readiness", _database_down)

    response = await client.get(READY_URL)

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": False, "redis": True},
    }


async def test_the_body_names_the_failing_dependency(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whoever is paged should not have to grep logs to learn what a 503 meant."""

    async def _mixed(_session_factory: object, _redis: object) -> dict[str, bool]:
        return {"database": True, "redis": False}

    monkeypatch.setattr(health, "check_readiness", _mixed)

    response = await client.get(READY_URL)

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["checks"] == {"database": True, "redis": False}


async def test_liveness_stays_independent_of_the_database(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The distinction M1 set up, now that there is a database to break.

    Liveness failing gets containers *restarted*. If it tracked the database,
    one blip would restart every pod in the deployment — turning a recoverable
    dependency outage into a full one.
    """

    async def _database_down(_session_factory: object, _redis: object) -> dict[str, bool]:
        return {"database": False, "redis": True}

    monkeypatch.setattr(health, "check_readiness", _database_down)

    assert (await client.get("/api/v1/health/live")).status_code == HTTPStatus.OK
    assert (await client.get(READY_URL)).status_code == HTTPStatus.SERVICE_UNAVAILABLE
