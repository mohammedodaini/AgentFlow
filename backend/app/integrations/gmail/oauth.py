# ruff: noqa: F401  — remove once this module is implemented (M12)
"""gmail OAuth specifics: scopes, authorize/token endpoints, refresh quirks."""

from __future__ import annotations

import httpx

from app.core.config import get_settings
from app.integrations.base import OAuthProvider

# TODO(M12): SCOPES; implement OAuthProvider for gmail
