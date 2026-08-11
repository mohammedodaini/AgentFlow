# ruff: noqa: F401  — remove once this module is implemented (M3)
"""POST /auth/* — register, login, refresh, logout.

Thin: parse schema → call app/auth/service.py → return schema.
No password or JWT logic here, ever.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.service import AuthService
from app.schemas.auth import LoginRequest, RegisterRequest, TokenPair

router = APIRouter(prefix="/auth", tags=["auth"])

# TODO(M3): POST /register — 201, creates user + personal org + owner membership
# TODO(M3): POST /login — TokenPair (access + refresh)
# TODO(M3): POST /refresh — rotate refresh token
# TODO(M3): POST /logout — revoke refresh token (jti denylist)
