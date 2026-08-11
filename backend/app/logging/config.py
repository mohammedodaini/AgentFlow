"""structlog configuration — called once from main.py before anything logs.

Layer: observability. Dev: pretty console renderer. Prod: JSON lines.
Every module logs via structlog.get_logger(); no stdlib logging.basicConfig.

Why JSON in production: log aggregators (CloudWatch, Loki, Datadog) index
fields, not prose. `logger.info("http.request", status_code=500)` is queryable;
an f-string log message is not.
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import Processor

from app.core.config import get_settings
from app.logging.processors import add_request_id


def _log_level_number(name: str) -> int:
    """Translate a level name from settings into structlog's numeric filter.

    Falls back to INFO rather than raising: a mistyped LOG_LEVEL should not be
    the reason production fails to boot.
    """
    return logging.getLevelNamesMapping().get(name.upper(), logging.INFO)


def configure_logging() -> None:
    """Install the processor chain. Idempotent — safe to call more than once."""
    settings = get_settings()

    shared: list[Processor] = [
        # Anything bound via structlog.contextvars.bind_contextvars(...) first.
        structlog.contextvars.merge_contextvars,
        add_request_id,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        # Renders exc_info into a traceback string when you log inside `except`.
        structlog.processors.format_exc_info,
    ]

    renderer: Processor = (
        structlog.dev.ConsoleRenderer()
        if settings.is_development
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(_log_level_number(settings.log_level)),
        # stdout, because 12-factor says a process writes its log stream there
        # and lets the platform handle routing, rotation, and shipping.
        logger_factory=structlog.WriteLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
