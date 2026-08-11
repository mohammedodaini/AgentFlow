"""Timing middleware — logs method, path, status, duration_ms per request.

Your first observability signal; later feeds monitoring/metrics.py.

One structured line per request is the cheapest useful telemetry there is: it
answers "is it up?", "is it slow?", and "what is erroring?" with no external
system attached yet.
"""

from __future__ import annotations

import time

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

logger = structlog.get_logger(__name__)


class TimingMiddleware(BaseHTTPMiddleware):
    """Measure and log how long each request took."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # perf_counter, not time.time(): monotonic, so an NTP correction
        # mid-request cannot produce a negative duration.
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000

        logger.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
        )
        return response
