# ruff: noqa: F401  — remove once this module is implemented (M11)
"""`oauth_tokens` — token secrets, SEPARATE from integration metadata.

Separate table so secrets carry stricter access controls and rotate without
touching integrations. Values are encrypted at the app layer BEFORE insert
(core/security.encrypt_secret) — never store plaintext tokens.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# TODO(M11): class OAuthToken(Base) — integration_id FK, access_token (encrypted),
#            refresh_token (encrypted), expires_at
