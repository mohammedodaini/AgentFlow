# ruff: noqa: F401  — remove once this module is implemented (M16)
"""Rate limiting (Redis sliding window, keyed by org). Deliberately LAST
milestone — premature rate limiting slows learning; production requires it."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware

# TODO(M16): class RateLimitMiddleware — Redis sliding window per org/IP, 429 + Retry-After
