"""Rate limiting: a Redis sliding window, keyed by who is asking.

Layer: middleware. Deliberately the last milestone — premature rate limiting
slows development, and production requires it.

**It fails open, and that is the decision worth arguing about.**
----------------------------------------------------------------
If Redis is unreachable, every request is allowed. The alternative — fail closed —
turns a Redis blip into a total outage, and Redis here is already the *optional*
dependency: the M3 denylist degrades to "a logged-out refresh token still works
until it expires", not to "nobody can log in".

The threat this defends against is abuse and runaway cost, not a determined
attacker with an unlimited budget. Against abuse, a limiter that is down for
ninety seconds costs ninety seconds of unmetered traffic. Failing closed costs a
full outage every time the cache restarts, which is a much larger and much more
frequent loss. A system where an authentication check depended on Redis would
deserve the opposite answer.

**Keyed by identity, not by IP where an identity exists.**
----------------------------------------------------------
IP is the wrong unit for an authenticated API: an office behind one NAT shares a
bucket, and an attacker with a /64 of IPv6 has effectively none. So the middleware
decodes the bearer token — stateless, no round trip, the same verification the
auth dependency will do a moment later — and keys on the user id. Only anonymous
traffic falls back to IP.

**The organization header is deliberately not used.** It is the obvious key: quota
per tenant is what a product would sell. But this middleware runs *before*
authentication, so `X-Organization-Id` at this point is an unverified string — and
keying on it would let anyone exhaust another tenant's quota by naming them. Per
-tenant quota belongs in a service that has already checked membership; this layer
answers a narrower question, which is whether one caller is making too many
requests.

**Cost is per route class, not per request.**
---------------------------------------------
An agent run costs a model call, a vector search and several database writes. A
health check costs nothing. One shared counter would either throttle dashboards
or let somebody run a thousand agent turns a minute, so expensive paths draw more
from the same bucket.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.core.security import decode_token

logger = structlog.get_logger(__name__)

WINDOW_SECONDS = 60
"""The sliding window. A minute is short enough that a user who hits the limit
waits a tolerable time, and long enough that a burst of page loads does not."""

KEY_PREFIX = "ratelimit:"

EXEMPT_PREFIXES = ("/api/v1/health", "/metrics", "/docs", "/openapi.json", "/redoc")
"""Paths that are never limited.

Health checks especially: an orchestrator polls them every few seconds from every
replica, and a limiter that 429s a liveness probe gets the container killed — the
limiter causing precisely the outage it exists to prevent.
"""

EXPENSIVE_PREFIXES = (
    "/api/v1/agent-runs",
    "/api/v1/ask",
    "/api/v1/approvals",
    "/api/v1/search",
    # **Added after a production audit, and it is not obvious from the name.**
    # `/auth` looks cheap: no model call, no vector search, one indexed SELECT.
    # It is the most CPU-expensive endpoint in the application, because Argon2 is
    # *designed* to be — 36.8ms per verification, paid even for an email that does
    # not exist (the timing equaliser in `AuthService.login`).
    #
    # At the default cost of 1 that allowed 300 password guesses a minute per
    # address, which is 11 seconds of CPU per minute from a single unauthenticated
    # client. At 5 it is 60 a minute, which no human reaches and a script cannot
    # turn into a denial of service.
    "/api/v1/auth",
)
"""Paths that draw more from the bucket. See the module docstring.

"Expensive" means expensive *to serve*, not slow to return. The three above cost
a model call and a vector scan; `/auth` costs a deliberately slow hash.
"""

EXPENSIVE_COST = 5
DEFAULT_COST = 1


class RateLimitMiddleware(BaseHTTPMiddleware):
    """A fixed-window counter in Redis, per caller per minute.

    A *fixed* window rather than a true sliding log, and the trade is stated
    plainly: at a window boundary a caller can spend two windows' budget in a
    couple of seconds. A sorted-set sliding log fixes that and costs an entry per
    request plus a trim on every check. For a limit whose purpose is bounding
    abuse and cost, twice the budget for one second is not the failure worth
    paying for on every request — and the fix, if it is ever needed, is this class
    and nothing else.
    """

    def __init__(self, app: Any, settings: Settings) -> None:
        super().__init__(app)
        self._limit = settings.rate_limit_per_minute
        self._enabled = settings.rate_limit_enabled

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self._enabled or _is_exempt(request.url.path):
            return await call_next(request)

        redis: Redis | None = getattr(request.app.state, "redis", None)

        if redis is None:
            # No Redis on the app at all — a test app, or startup not finished.
            return await call_next(request)

        identity = _identity(request)
        cost = EXPENSIVE_COST if request.url.path.startswith(EXPENSIVE_PREFIXES) else DEFAULT_COST
        window = int(time.time()) // WINDOW_SECONDS
        key = f"{KEY_PREFIX}{identity}:{window}"

        try:
            # Pipelined: INCRBY and EXPIRE in one round trip. Two calls would mean
            # a key that survives a crash between them — with no TTL, forever,
            # holding a count that never resets and locking that caller out
            # permanently.
            pipeline = redis.pipeline()
            pipeline.incrby(key, cost)
            # Set on every request rather than only on creation. `EXPIRE` on an
            # existing key is idempotent, and the alternative (`NX`) leaves a key
            # created by a lost race with no expiry at all.
            pipeline.expire(key, WINDOW_SECONDS * 2)
            used = int((await pipeline.execute())[0])
        except RedisError:
            # Fail open. See the module docstring — this is the considered choice.
            logger.warning("ratelimit.unavailable", path=request.url.path)
            return await call_next(request)

        if used > self._limit:
            retry_after = WINDOW_SECONDS - int(time.time()) % WINDOW_SECONDS
            logger.warning(
                "ratelimit.exceeded",
                identity=identity,
                path=request.url.path,
                used=used,
                limit=self._limit,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limited",
                        "message": (f"Too many requests. Try again in {retry_after} seconds."),
                    }
                },
                # RFC 6585 §4. Without it a client has to guess, and clients that
                # guess retry immediately — turning a rate limit into a hot loop.
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(self._limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        # Published so a well-behaved client can slow down before being refused.
        # A limit nobody can see is one every client discovers by hitting it.
        response.headers["X-RateLimit-Limit"] = str(self._limit)
        response.headers["X-RateLimit-Remaining"] = str(max(self._limit - used, 0))
        return response


def _is_exempt(path: str) -> bool:
    return path.startswith(EXEMPT_PREFIXES)


def _identity(request: Request) -> str:
    """Who is being limited: a verified user id, or an IP address.

    The token is *verified*, not merely parsed. An unverified `sub` would let
    anyone claim to be anybody — which here means claiming somebody else's bucket
    and exhausting it for them, a denial of service delivered through the thing
    meant to prevent one.
    """
    header = request.headers.get("authorization", "")

    if header.startswith("Bearer "):
        try:
            claims = decode_token(header.removeprefix("Bearer "), expected_type="access")
            return f"user:{claims['sub']}"
        except AuthenticationError:
            # Expired, forged, or the wrong token type. Not an identity, so this
            # request is limited by address like any other anonymous one — and it
            # is *not* logged here: an expired token is the most ordinary event in
            # a session's life, and a warning per occurrence would bury the real
            # signal. `/auth/refresh` is where that gets handled.
            logger.debug("ratelimit.anonymous", reason="invalid_token")

    return f"ip:{client_ip(request)}"


def client_ip(request: Request) -> str:
    """The caller's address, trusting `X-Forwarded-For` only for its *first* entry.

    A proxy appends; a client can send whatever it likes. Reading the last entry —
    or joining them — lets a caller add fake hops and rotate through an unlimited
    supply of identities. The first entry is the one the outermost trusted proxy
    saw, and it is the only one worth reading.

    Stated honestly: this is correct **only behind a proxy that overwrites or
    appends to the header**. Exposed directly to the internet, a client can forge
    it outright, and the fallback below is the safe configuration.

    Public because the audit trail wants the same answer this limiter does. Two
    implementations of "where did this come from" would eventually disagree, and
    the disagreement would surface as a security event attributed to the wrong
    address.
    """
    forwarded = request.headers.get("x-forwarded-for")

    if forwarded:
        return forwarded.split(",")[0].strip()

    return request.client.host if request.client else "unknown"
