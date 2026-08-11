"""First e2e test — proves the app factory + /health wiring (M1).

e2e = through HTTP via httpx.AsyncClient against the ASGI app; no mocks.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.middleware.request_id import REQUEST_ID_HEADER


async def test_health_live_returns_200(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_request_id_is_echoed_when_supplied(client: AsyncClient) -> None:
    """An upstream trace ID survives the hop into this service."""
    response = await client.get(
        "/api/v1/health/live",
        headers={REQUEST_ID_HEADER: "trace-me-123"},
    )

    assert response.headers[REQUEST_ID_HEADER] == "trace-me-123"


async def test_request_id_is_generated_when_absent(client: AsyncClient) -> None:
    """Every response carries an ID, even when the client sent none."""
    response = await client.get("/api/v1/health/live")

    assert response.headers[REQUEST_ID_HEADER]


async def test_unknown_path_returns_404(client: AsyncClient) -> None:
    """Guards against a router-prefix regression silently swallowing routes."""
    response = await client.get("/api/v1/health/nope")

    assert response.status_code == 404
