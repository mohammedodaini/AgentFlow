# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M11)
"""google_calendar API client — the ONLY code that speaks google_calendar's wire format.

Consumed by services and agent tools; returns OUR domain shapes, never raw
provider payloads.
"""

from __future__ import annotations

import httpx

from app.integrations.base import BaseClient

# TODO(M11): GoogleCalendarClient — list_events, find_availability, create_event (approval-gated)
