# ruff: noqa: F401  — remove once this module is implemented (M11)
"""Shared integration contracts: base client + OAuth provider interface.

Rule: integration types (e.g. a Gmail message dict) NEVER leak upward —
each client translates provider payloads into our own schemas at the boundary.
"""

from __future__ import annotations

from typing import Protocol

import httpx

# TODO(M11): class OAuthProvider(Protocol) — authorize_url(state, scopes),
#            exchange_code(code), refresh(refresh_token)
# TODO(M11): class BaseClient — shared httpx.AsyncClient wiring, retries, timeouts
