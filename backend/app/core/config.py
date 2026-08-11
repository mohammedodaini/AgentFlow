"""Settings — single source of runtime configuration (12-factor).

Layer: core (leaf — imports nothing else from app/).
Reads .env via pydantic-settings; every other module asks get_settings(),
never os.environ directly.

Why a leaf: config is imported by nearly everything, so if it imported back
into the app the import graph would cycle. Keeping it dependency-free is what
lets db, logging, api and the workers all read it safely.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]
"""The only three environments that exist. A typo fails at startup, not later."""


class Settings(BaseSettings):
    """Every runtime knob the app has, validated once at process start.

    Defaults describe a working *local* setup so `make dev` runs on a fresh
    clone. Production overrides everything through real environment variables —
    which is precisely the 12-factor contract.
    """

    model_config = SettingsConfigDict(
        # Both locations are supported: the repo-root .env documented in
        # README/.env.example, and a backend-local .env if you prefer one.
        # Later entries win, and real environment variables beat both.
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        # .env carries keys for milestones we haven't built yet (ANTHROPIC_API_KEY,
        # STRIPE_API_KEY, ...). Ignoring unknown keys keeps M1 from rejecting them.
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    app_name: str = "agentflow"
    env: Environment = Field(default="development", validation_alias="APP_ENV")
    debug: bool = False
    log_level: str = "INFO"

    # --- Infrastructure (consumed from M2 onward; declared here so the whole
    #     configuration contract lives in one readable place) ---
    database_url: str = "postgresql+asyncpg://agentflow:agentflow@localhost:5432/agentflow"
    redis_url: str = "redis://localhost:6379/0"

    @property
    def is_development(self) -> bool:
        """True only in local development — drives human-readable log output."""
        return self.env == "development"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, parsed once.

    Cached so it is cheap to use as a FastAPI dependency: injecting settings
    into a route costs a dict lookup rather than re-reading and re-validating
    the environment on every request.

    Tests call `get_settings.cache_clear()` to reset it between cases.
    """
    return Settings()
