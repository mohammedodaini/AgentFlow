# ruff: noqa: F401  — remove once this module is implemented (M1)
"""Request-ID middleware — first in the stack.

Generates/propagates X-Request-ID into a contextvar; the structlog processor
(app/logging/processors.py) stamps it on every log line. This is how you
trace one request across API and worker logs.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware

# TODO(M1): request_id_var: ContextVar[str]
# TODO(M1): class RequestIDMiddleware — read incoming header or uuid4, set var,
#           echo header on the response
