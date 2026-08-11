# ruff: noqa: F401  — remove once this module is implemented (M1)
"""structlog configuration — called once from main.py before anything logs.

Layer: observability. Dev: pretty console renderer. Prod: JSON lines.
Every module logs via structlog.get_logger(); no stdlib logging.basicConfig.
"""

from __future__ import annotations

import structlog

from app.core.config import get_settings
from app.logging.processors import add_request_id

# TODO(M1): configure_logging() — processor chain (timestamper, add_request_id,
#           exc_info renderer), JSON vs console by settings.env
