# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M3)
"""Refresh-token lifecycle: rotation and revocation (jti denylist in Redis).

Separate from core/security (pure JWT encode/decode) because rotation policy
is a flow decision, not a primitive.
"""

from __future__ import annotations

from app.core.security import create_token, decode_token

# TODO(M3): issue_pair(user_id) -> (access, refresh)
# TODO(M3): rotate(refresh_token) — old jti revoked, new pair issued (replay defense)
# TODO(M3): revoke(jti) / is_revoked(jti) — Redis set with TTL = token TTL
