"""Request-ID middleware — first in the stack.

Generates/propagates X-Request-ID into a contextvar; the structlog processor
(app/logging/processors.py) stamps it on every log line. This is how you
trace one request across API and worker logs.

A ContextVar rather than a global: each concurrent request gets its own value
under asyncio, which a module-level variable could never provide.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
"""Current request's ID, or "" outside a request (worker/startup code)."""


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Assigns every request an ID and echoes it back on the response.

    Honours an inbound X-Request-ID so a trace started by a proxy, the
    frontend, or another service survives the hop into this app; mints a
    uuid4 when there isn't one.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            # Reset even when the handler raises, or the ID leaks into whatever
            # task reuses this context next.
            request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
