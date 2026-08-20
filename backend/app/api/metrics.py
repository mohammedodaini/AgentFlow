"""`GET /metrics` — the Prometheus scrape endpoint.

Layer: api. Mounted at the root rather than under `/api/v1`, because a scrape
endpoint is infrastructure rather than product: it is not versioned with the API,
and moving it in a v2 would silently stop every dashboard.

**Authenticated by a shared token, not by a JWT.** Prometheus cannot log in,
refresh, or carry an organization header — a static bearer is what scrapers
actually support. The token is refused as empty in production (`Settings`), and
optional in development because a container scraped over a private network has
nothing to protect from.

The comparison is constant-time. That is close to superstition for a
metrics token — the timing signal over a network is buried in noise — and it costs
one function call, so there is no reason to write the version that leaks.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError, NotFoundError
from app.monitoring.metrics import MetricsRegistry, get_registry

router = APIRouter(tags=["monitoring"])

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
"""The exposition format's declared content type. Prometheus is lenient about it
and other scrapers are not."""


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
async def metrics(
    settings: Annotated[Settings, Depends(get_settings)],
    registry: Annotated[MetricsRegistry, Depends(get_registry)],
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """Render every counter this process holds.

    `include_in_schema=False`: this is not part of the product's API, and putting
    it in the public OpenAPI document advertises its existence to anybody reading
    `/docs`.

    A disabled endpoint answers **404, not 403**. 403 confirms the endpoint exists
    and is merely closed, which is exactly the reconnaissance the token is there
    to deny.
    """
    if not settings.metrics_enabled:
        message = "Not found."
        raise NotFoundError(message)

    expected = settings.metrics_token.get_secret_value()

    if expected:
        supplied = (authorization or "").removeprefix("Bearer ")

        if not hmac.compare_digest(supplied, expected):
            message = "Not authorized to read metrics."
            raise AuthenticationError(message)

    return Response(content=registry.render(), media_type=CONTENT_TYPE)
