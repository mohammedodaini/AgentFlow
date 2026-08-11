# ruff: noqa: F401  — remove once this module is implemented (M1)
"""Settings — single source of runtime configuration (12-factor).

Layer: core (leaf — imports nothing else from app/).
Reads .env via pydantic-settings; every other module asks get_settings(),
never os.environ directly.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# TODO(M1): class Settings(BaseSettings) — app_name, env (dev|test|prod), debug,
#           database_url, redis_url, log_level
# TODO(M3): jwt_secret, jwt_algorithm, access_token_ttl, refresh_token_ttl
# TODO(M5): storage settings (object-store bucket/path)
# TODO(M7): anthropic_api_key, embedding model name
# TODO(M11): google_client_id/secret, token_encryption_key
# TODO(M1): @lru_cache get_settings() -> Settings — cached so it's a cheap dependency
