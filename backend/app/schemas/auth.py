# ruff: noqa: F401  — remove once this module is implemented (M3)
"""Auth request/response shapes."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, SecretStr

# TODO(M3): RegisterRequest — email, password (min length, validated), full_name
# TODO(M3): LoginRequest — email, password
# TODO(M3): TokenPair — access_token, refresh_token, token_type="bearer"
