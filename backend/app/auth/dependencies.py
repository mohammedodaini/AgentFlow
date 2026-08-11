# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M3)
"""FastAPI auth dependencies — the gate every protected route declares.

Dependencies (not middleware) because they are per-route, testable, and
composable: get_current_user -> get_current_membership -> require_role.
"""

from __future__ import annotations

from fastapi import Depends
from fastapi.security import HTTPBearer

from app.core.security import decode_token
from app.db.deps import get_db
from app.models.membership import Membership
from app.models.user import User

# TODO(M3): get_current_user — bearer token -> User (401 on anything wrong)
# TODO(M3): get_current_membership — user + X-Organization-Id header -> Membership (403)
# TODO(M3): require_role(*roles) — dependency factory for owner/admin-only routes
