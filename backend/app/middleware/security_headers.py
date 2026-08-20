"""Response headers that turn browser defaults into safer ones.

Layer: middleware. Cheap, boring, and the part of a hardening pass most often
skipped because nothing breaks without it — which is exactly the point: nothing
breaks *visibly*.

What each one is actually for
------------------------------
**`X-Content-Type-Options: nosniff`** stops a browser second-guessing a declared
content type. Without it, a text file a user uploaded and later fetched can be
sniffed as HTML and executed in this origin. M5 lets users upload files; that
makes this the header with the most direct bearing on this product.

**`Referrer-Policy: strict-origin-when-cross-origin`** stops full URLs leaking to
third parties. Paths here contain ids — `/runs/<uuid>`, `/approvals/<uuid>` — and
a default referrer policy sends the whole path to every external host a page
touches.

**`X-Frame-Options: DENY`** blocks clickjacking. The approvals inbox is a page
whose entire function is a button that authorises a side effect, which is the
canonical clickjacking target.

**`Content-Security-Policy`** on API responses is close to symbolic — JSON does
not execute — and it costs nothing to send a policy that forbids everything, which
covers the error pages and docs that *are* HTML.

**HSTS only in production, and only over TLS.** Sending it in development would
pin `localhost` to HTTPS in the developer's browser, which is a self-inflicted
outage that survives clearing the cache and takes an obscure `chrome://net-internals`
visit to undo. It is also meaningless over plain HTTP, so it is emitted only when
both conditions hold.

What is deliberately absent
----------------------------
**No CORS headers.** Not an oversight: ADR-0016 put the browser's tokens in
httpOnly cookies and made every write a Server Action, so the browser never calls
this API directly and there is no cross-origin request to permit. A permissive
`Access-Control-Allow-Origin` added "just in case" is how an API that needed no
CORS acquires a cross-origin attack surface.
"""

from __future__ import annotations

from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
"""Forbid everything. This API returns JSON; the only HTML it serves is FastAPI's
docs and error pages, and neither needs to load anything."""

HSTS = "max-age=31536000; includeSubDomains"
"""One year, subdomains included. No `preload`: that submits the domain to a list
browsers hard-code, and removal takes months — a decision for whoever owns the
domain, not for a middleware default."""


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add hardening headers to every response."""

    def __init__(self, app: Any, *, production: bool) -> None:
        super().__init__(app)
        self._production = production

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", API_CSP)
        # Removes the one thing a scanner reads first. Not security by itself —
        # anyone can fingerprint a server from its behaviour — but publishing the
        # exact version turns "try everything" into "try the three CVEs for this
        # build", and it costs one line to stop.
        response.headers["Server"] = "agentflow"

        if self._production and request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", HSTS)

        return response
