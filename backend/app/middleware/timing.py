"""Timing middleware — one log line and one metric observation per request.

Your first observability signal; from M16 it also feeds `monitoring/metrics.py`,
which is what the module docstring always said it would.

One structured line per request is the cheapest useful telemetry there is: it
answers "is it up?", "is it slow?", and "what is erroring?" with no external
system attached yet. The metric is the same three facts aggregated, which is what
makes them alertable — you cannot page on a log line without shipping every log
line somewhere that can count them.

**Both come from one measurement, deliberately.** A separate metrics middleware
would time a slightly different span — its own wrapper is inside or outside this
one — and two numbers that should agree and do not is the kind of discrepancy
somebody spends an afternoon on. It also means the route template is read once,
after `call_next` has populated it.
"""

from __future__ import annotations

import time

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.monitoring.metrics import MetricsRegistry, route_template

logger = structlog.get_logger(__name__)


class TimingMiddleware(BaseHTTPMiddleware):
    """Measure how long each request took; log it and count it."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # perf_counter, not time.time(): monotonic, so an NTP correction
        # mid-request cannot produce a negative duration.
        start = time.perf_counter()
        response = await call_next(request)
        seconds = time.perf_counter() - start

        logger.info(
            "http.request",
            method=request.method,
            # The concrete path in the log, the *template* in the metric below.
            # A log line is read by a person asking about one request; a metric
            # label carrying a UUID is one time series per request, which is how
            # a metrics endpoint takes down the process it was added to observe.
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(seconds * 1000, 2),
        )

        registry: MetricsRegistry | None = getattr(request.app.state, "metrics", None)

        if registry is not None:
            registry.observe_request(
                request.method, route_template(request), response.status_code, seconds
            )

        return response
