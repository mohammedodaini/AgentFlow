# ruff: noqa: F401  — remove once this module is implemented (M3)
"""Auth FLOWS: register, login, refresh, logout.

Uses core/security primitives; owns the transaction (register = user +
personal org + owner membership + audit event, atomically).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationError, DuplicateEmailError
from app.core.security import create_token, hash_password, verify_password
from app.models.membership import Membership
from app.models.organization import Organization
from app.models.user import User

# TODO(M3): class AuthService — register, login (vague error on bad creds:
#           never reveal which of email/password was wrong), refresh, logout
