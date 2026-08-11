# ruff: noqa: F401  — remove once this module is implemented (M11)
"""/integrations — OAuth connect flow + connection management.

GET /{provider}/connect returns the authorize URL; /callback exchanges the
code, encrypts tokens, stores integration + oauth_tokens rows.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.schemas.integration import IntegrationRead
from app.services.integration_service import IntegrationService

router = APIRouter(prefix="/integrations", tags=["integrations"])

# TODO(M11): GET / — connected integrations · GET /{provider}/connect
# TODO(M11): GET /{provider}/callback — code exchange (state param = CSRF protection)
# TODO(M11): DELETE /{id} — disconnect + revoke tokens upstream
