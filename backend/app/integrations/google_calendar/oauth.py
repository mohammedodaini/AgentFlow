# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M11)
"""google_calendar OAuth specifics: scopes, authorize/token endpoints, refresh quirks."""

from __future__ import annotations

import httpx

from app.core.config import get_settings
from app.integrations.base import OAuthProvider

# TODO(M11): SCOPES; implement OAuthProvider for google_calendar
