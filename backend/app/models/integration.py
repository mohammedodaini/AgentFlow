# ruff: noqa: F401  — remove once this module is implemented (M11)
"""`integrations` — one row per connected external product per org."""

from __future__ import annotations

import enum

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

# TODO(M11): class Provider(enum.StrEnum) — gmail | google_calendar | google_drive |
#            slack | notion | github | stripe
# TODO(M11): class Integration(Base) — organization_id FK, provider, status,
#            connected_by FK users, scopes, external_account_id;
#            relationship oauth_tokens -> OAuthToken
