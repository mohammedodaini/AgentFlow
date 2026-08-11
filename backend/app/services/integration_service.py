# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M11)
"""Integration business logic: OAuth connect/callback orchestration,
token refresh, disconnect. Delegates provider specifics to
app/integrations/<provider>/oauth.py; owns the DB rows + encryption calls."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.security import decrypt_secret, encrypt_secret
from app.models.integration import Integration
from app.models.oauth_token import OAuthToken

# TODO(M11): class IntegrationService — begin_connect(org_id, provider) -> auth URL,
#            complete_callback(state, code), get_fresh_token(integration_id)
#            (auto-refresh when expired), disconnect(id)
