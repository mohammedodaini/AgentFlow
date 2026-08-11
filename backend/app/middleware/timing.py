# ruff: noqa: F401  — remove once this module is implemented (M1)
"""Timing middleware — logs method, path, status, duration_ms per request.

Your first observability signal; later feeds monitoring/metrics.py.
"""

from __future__ import annotations

import time

import structlog
from starlette.middleware.base import BaseHTTPMiddleware

# TODO(M1): class TimingMiddleware — perf_counter around call_next, structured log
