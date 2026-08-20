"""Rate limiting, security headers and the metrics endpoint over HTTP (M16).

The rate-limit tests are the only place in the suite where the limiter is on:
`tests/conftest.py` disables it globally, because a per-caller counter in a shared
Redis would make the hundredth test fail for something the first test did — and
*which* test broke would depend on collection order.

So these tests turn it back on for themselves, and each one uses a fresh identity
so they cannot exhaust each other's budget.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from http import HTTPStatus

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import get_settings
from app.main import create_app
from tests.e2e.test_search_api import register


@pytest.fixture
async def limited_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    """An app with rate limiting on and a tiny budget.

    A separate application rather than the shared `client` fixture: the limit is
    read once when the middleware is constructed, so changing the setting after
    the app exists would have no effect — and a test that silently measures the
    default 300 would pass while asserting nothing.
    """
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "3")
    get_settings.cache_clear()

    app = create_app()

    async with app.router.lifespan_context(app):
        await app.state.redis.flushdb()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client


# --- rate limiting -------------------------------------------------------


async def test_too_many_requests_are_refused(limited_client: AsyncClient) -> None:
    """The point of the milestone: a caller cannot spend without limit."""
    seen: list[int] = []

    for _ in range(6):
        response = await limited_client.get(
            "/api/v1/organizations", headers={"X-Test-Id": str(uuid.uuid4())}
        )
        seen.append(response.status_code)

    assert HTTPStatus.TOO_MANY_REQUESTS in seen


async def test_a_refusal_says_when_to_come_back(limited_client: AsyncClient) -> None:
    """RFC 6585 §4. Without `Retry-After` a client has to guess, and clients that
    guess retry immediately — turning a rate limit into a hot loop, which is more
    load than the traffic being limited."""
    for _ in range(8):
        response = await limited_client.get("/api/v1/organizations")

        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            assert int(response.headers["Retry-After"]) > 0
            assert response.headers["X-RateLimit-Remaining"] == "0"
            assert response.json()["error"]["code"] == "rate_limited"
            return

    pytest.fail("the limiter never fired")


async def test_health_checks_are_never_limited(limited_client: AsyncClient) -> None:
    """**The one that would cause the outage it exists to prevent.**

    An orchestrator polls liveness every few seconds from every replica. A limiter
    that 429s a liveness probe gets the container killed, and the restart loop
    looks exactly like an application crash.
    """
    for _ in range(20):
        response = await limited_client.get("/api/v1/health/live")

        assert response.status_code == HTTPStatus.OK


async def test_the_limit_is_published_on_success(limited_client: AsyncClient) -> None:
    """A limit nobody can see is one every client discovers by hitting it."""
    response = await limited_client.get("/api/v1/health/ready")
    # Exempt paths carry no headers; an ordinary one does.
    response = await limited_client.get("/api/v1/organizations")

    assert response.headers["X-RateLimit-Limit"] == "3"


async def test_expensive_paths_cost_more(limited_client: AsyncClient) -> None:
    """An agent run costs a model call, a vector search and several writes; a
    listing costs a query. One shared counter would either throttle dashboards or
    permit a thousand agent turns a minute.

    With a budget of three, one agent-run request (cost 5) is refused outright
    while three listings (cost 1) are not — which is the weighting, demonstrated
    rather than described.
    """
    expensive = await limited_client.post(
        "/api/v1/agent-runs/supervised", json={"instruction": "How are expenses paid?"}
    )

    assert expensive.status_code == HTTPStatus.TOO_MANY_REQUESTS


async def test_cheap_paths_fit_inside_the_same_budget(limited_client: AsyncClient) -> None:
    """The other half of the comparison: three cheap requests fit where one
    expensive one does not."""
    for _ in range(3):
        response = await limited_client.get("/api/v1/organizations")

        assert response.status_code != HTTPStatus.TOO_MANY_REQUESTS


async def test_the_limiter_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """**The decision worth arguing about, asserted.**

    If Redis is unreachable every request is allowed. Failing closed turns a Redis
    blip into a total outage, and the threat here is abuse and runaway cost rather
    than an attacker with an unlimited budget — ninety seconds of unmetered
    traffic against a full outage on every cache restart is not a close trade.
    """
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    get_settings.cache_clear()

    app = create_app()

    async with app.router.lifespan_context(app):

        def explode(*args: object, **kwargs: object) -> None:
            raise RedisConnectionError("redis is down")

        monkeypatch.setattr(app.state.redis, "pipeline", explode)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(5):
                response = await client.get("/api/v1/health/ready")
                assert response.status_code != HTTPStatus.TOO_MANY_REQUESTS

            # And an ordinary path, which the limiter does inspect.
            response = await client.get("/api/v1/organizations")
            assert response.status_code != HTTPStatus.TOO_MANY_REQUESTS


# --- security headers ----------------------------------------------------


async def test_every_response_is_hardened(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/live")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]


async def test_an_error_response_is_hardened_too(client: AsyncClient) -> None:
    """Error pages are the HTML this API actually serves, so they are the ones a
    content-type sniffing attack would use."""
    response = await client.get("/api/v1/nothing-here")

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.headers["X-Content-Type-Options"] == "nosniff"


async def test_a_rate_limited_response_is_hardened(limited_client: AsyncClient) -> None:
    """**The middleware-ordering bug this asserts against.**

    The limiter short-circuits with its own response. Registered *inside* the
    headers middleware, that response would never pass through it — so the one
    reply an abusive client receives most often would be the only unhardened one
    in the application.
    """
    for _ in range(8):
        response = await limited_client.get("/api/v1/organizations")

        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            return

    pytest.fail("the limiter never fired")


async def test_hsts_is_absent_in_development(client: AsyncClient) -> None:
    """Sending it over plain HTTP would pin localhost to HTTPS in the developer's
    browser — a self-inflicted outage that survives clearing the cache."""
    response = await client.get("/api/v1/health/live")

    assert "Strict-Transport-Security" not in response.headers


async def test_the_server_header_says_nothing_useful(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/live")

    assert response.headers["Server"] == "agentflow"
    assert "uvicorn" not in response.headers["Server"].lower()


async def test_no_cors_headers_are_sent(client: AsyncClient) -> None:
    """Not an oversight (ADR-0016). The browser never calls this API directly —
    tokens are httpOnly cookies and writes are Server Actions — so there is no
    cross-origin request to permit, and a permissive header added "just in case"
    is how an API that needed no CORS acquires a cross-origin attack surface."""
    response = await client.get("/api/v1/health/live", headers={"Origin": "https://evil.example"})

    assert "Access-Control-Allow-Origin" not in response.headers


# --- metrics -------------------------------------------------------------


async def test_metrics_are_served(client: AsyncClient) -> None:
    await client.get("/api/v1/health/live")

    response = await client.get("/metrics")

    assert response.status_code == HTTPStatus.OK
    assert "agentflow_http_requests_total" in response.text
    assert "agentflow_uptime_seconds" in response.text


async def test_metrics_label_the_route_not_the_path(client: AsyncClient) -> None:
    """**The line between a metrics endpoint and an outage.**

    A path label containing a UUID is one time series per request. Memory grows
    without limit and the scrape eventually times out — the endpoint added to
    observe the process being what takes it down.
    """
    headers = await register(client)
    run = await client.post(
        "/api/v1/agent-runs/supervised",
        headers=headers,
        json={"instruction": "How are expenses reimbursed?"},
    )
    run_id = run.json()["run"]["id"]
    await client.get(f"/api/v1/agent-runs/{run_id}", headers=headers)

    body = (await client.get("/metrics")).text

    assert 'route="/api/v1/agent-runs/{run_id}"' in body
    assert run_id not in body


async def test_unmatched_paths_share_one_series(client: AsyncClient) -> None:
    """A scanner probing a thousand random paths must not create a thousand
    series."""
    for index in range(3):
        await client.get(f"/api/v1/{uuid.uuid4()}/{index}")

    body = (await client.get("/metrics")).text

    assert 'route="unmatched"' in body


async def test_metrics_require_the_token_when_one_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("METRICS_TOKEN", "s3cret")
    get_settings.cache_clear()
    app = create_app()

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/metrics")).status_code == HTTPStatus.UNAUTHORIZED

        authorized = await client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
        assert authorized.status_code == HTTPStatus.OK


async def test_a_disabled_endpoint_is_a_404_not_a_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 confirms the endpoint exists and is merely closed, which is exactly the
    reconnaissance the token exists to deny."""
    monkeypatch.setenv("METRICS_ENABLED", "false")
    get_settings.cache_clear()
    app = create_app()

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/metrics")).status_code == HTTPStatus.NOT_FOUND


async def test_metrics_are_not_in_the_public_schema(app: FastAPI) -> None:
    """Putting it in the OpenAPI document advertises it to anyone reading /docs."""
    assert "/metrics" not in app.openapi()["paths"]


async def test_agent_spend_reaches_the_metrics(client: AsyncClient) -> None:
    """**The counters this product actually pays for, end to end.**

    They existed and were never emitted for most of M16 — `observe_agent_run` was
    tested in isolation while nothing called it, which is a dashboard panel that
    never fills. A reader cannot tell "no agent ran" from "this is not measured",
    and the second is the dangerous reading.
    """
    headers = await register(client)
    await client.post(
        "/api/v1/agent-runs/supervised",
        headers=headers,
        json={"instruction": "How are expenses reimbursed?"},
    )

    body = (await client.get("/metrics")).text

    assert 'agentflow_agent_runs_total{agent="supervisor",status="succeeded"}' in body
    assert 'agentflow_agent_runs_total{agent="rag",status="succeeded"}' in body
    assert "agentflow_agent_cost_usd_total" in body
