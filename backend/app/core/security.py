# ruff: noqa: F401  — remove once this module is implemented (M3)
"""Security primitives — hashing, JWT encode/decode, secret encryption.

Layer: core (leaf). Pure functions only; no DB, no request context.
The auth *feature* (flows, dependencies) lives in app/auth/ and uses these.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher

# TODO(M3): hash_password(plain) / verify_password(plain, hashed) — Argon2id
# TODO(M3): create_token(subject, ttl, token_type) -> str  (jti for refresh revocation)
# TODO(M3): decode_token(token) -> payload — raises AuthenticationError on bad/expired
# TODO(M11): encrypt_secret(value) / decrypt_secret(value) — for oauth_tokens at rest
