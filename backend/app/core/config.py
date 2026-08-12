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
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]
"""The only three environments that exist. A typo fails at startup, not later."""

PLACEHOLDER_SECRET = "dev-only-insecure-secret-change-me-before-production"  # noqa: S105
"""The value shipped in .env.example. Refused in production — see the validator below.

Long enough to clear MINIMUM_SECRET_BYTES so local runs are quiet: a short
default makes PyJWT warn on every single test, and a warning that always fires
is a warning nobody reads.
"""

MINIMUM_SECRET_BYTES = 32
"""RFC 7518 §3.2: an HMAC key for HS256 must be at least as long as the hash
output. A shorter key does not make the signature *look* invalid — it just
quietly shrinks the search space for anyone brute-forcing it."""


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

    # --- Connection pool (M2) ---
    # Postgres' own max_connections is the ceiling every client shares. The
    # arithmetic that matters: (db_pool_size + db_max_overflow) x number of
    # processes must stay under it, or one traffic spike exhausts the server
    # and *every* service starts failing, not just this one.
    db_pool_size: int = 5
    """Connections held open per process. Async workers need far fewer than sync ones."""

    db_max_overflow: int = 10
    """Extra connections allowed during a burst, closed again once idle."""

    db_echo: bool = False
    """Log every emitted statement. Priceless when debugging a query, ruinous in production."""

    # --- Auth (M3) ---
    secret_key: SecretStr = SecretStr(PLACEHOLDER_SECRET)
    """Signs every JWT. `openssl rand -hex 32`.

    `SecretStr` so it cannot leak through a `repr()`, a log line, or a
    validation error — all three of which print settings objects.
    """

    jwt_algorithm: str = "HS256"
    """Symmetric: one key both signs and verifies.

    Correct while a single service issues and validates its own tokens. The day
    another service needs to *verify* without being able to *mint*, this moves
    to RS256 and the verifier gets only the public key.
    """

    access_token_expire_minutes: int = 30
    """Short, because access tokens cannot be revoked — see app/auth/tokens.py."""

    refresh_token_expire_days: int = 7
    """Long, and revocable, which is the trade that makes the short access TTL bearable."""

    # --- Object storage (M5) ---
    storage_backend: Literal["local"] = "local"
    """Which `ObjectStorage` implementation to build. A `Literal`, so a typo in
    the environment fails at startup — the same reason `env` is one."""

    storage_local_path: str = "./var/storage"
    """Root directory for the local backend. Under `var/` by convention: the
    place a twelve-factor app keeps state it did not get from configuration.
    Gitignored, because uploaded documents must never reach a repository."""

    max_upload_bytes: int = 25 * 1024 * 1024
    """25 MiB. Enforced while *reading* the upload, not after it.

    The distinction is the whole point. Checking `Content-Length` trusts the
    client, and checking the size once the file has been read means the server
    already spent the disk and memory an attacker wanted it to spend. The
    ceiling is a policy question rather than a technical one; 25 MiB covers a
    long PDF report and refuses a video.
    """

    allowed_upload_mime_types: list[str] = [
        "application/pdf",
        "text/plain",
        "text/markdown",
    ]
    """An allowlist, because a denylist of dangerous types is unbounded.

    Only types `app/rag/ingestion.py` can actually extract text from. Accepting
    a `.docx` we cannot parse would mean storing bytes, charging the user for
    them, and answering `status=failed` — better to refuse at the door with 415
    and say which types work.
    """

    # --- Background work (M5) ---
    arq_max_tries: int = 3
    """Attempts before arq gives up on a job.

    Retries exist for the transient half of the failure space — Redis blipped,
    Postgres failed over. They do nothing for the permanent half: a corrupt PDF
    fails identically all three times. That is why the ingestion task marks a
    document `failed` itself rather than letting the retries quietly run out.
    """

    arq_job_timeout_seconds: int = 300
    """Five minutes. A job with no timeout does not fail — it *hangs*, holding
    a worker slot forever, and the queue behind it stops moving."""

    @property
    def is_development(self) -> bool:
        """True only in local development — drives human-readable log output."""
        return self.env == "development"

    @model_validator(mode="after")
    def _reject_placeholder_secret_in_production(self) -> Self:
        """Refuse to start a production process that signs tokens with a known key.

        This is the single highest-value line in the file. A leaked or
        default signing key means anyone can mint a token for any user, and
        nothing in the application would notice — every request would look
        perfectly legitimate. Failing at startup is loud; the alternative
        fails silently, in production, for as long as nobody looks.
        """
        if self.env != "production":
            return self

        secret = self.secret_key.get_secret_value()

        if secret == PLACEHOLDER_SECRET:
            message = (
                "SECRET_KEY is still the placeholder value in production. "
                "Generate one with: openssl rand -hex 32"
            )
            raise ValueError(message)

        if len(secret.encode()) < MINIMUM_SECRET_BYTES:
            message = (
                f"SECRET_KEY must be at least {MINIMUM_SECRET_BYTES} bytes for "
                f"{self.jwt_algorithm} (RFC 7518 §3.2); got {len(secret.encode())}. "
                "Generate one with: openssl rand -hex 32"
            )
            raise ValueError(message)

        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, parsed once.

    Cached so it is cheap to use as a FastAPI dependency: injecting settings
    into a route costs a dict lookup rather than re-reading and re-validating
    the environment on every request.

    Tests call `get_settings.cache_clear()` to reset it between cases.
    """
    return Settings()
