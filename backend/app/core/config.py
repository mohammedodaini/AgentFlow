"""Settings — single source of runtime configuration (12-factor).

Layer: core (leaf — imports nothing else from app/).
Reads .env via pydantic-settings; every other module asks get_settings(),
never os.environ directly.

Why a leaf: config is imported by nearly everything, so if it imported back
into the app the import graph would cycle. Keeping it dependency-free is what
lets db, logging, api and the workers all read it safely.
"""

from __future__ import annotations

from decimal import Decimal
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

PLACEHOLDER_ENCRYPTION_KEY = "YWdlbnRmbG93LWRldi1vbmx5LWluc2VjdXJlLWtleSE="  # noqa: S105
"""The Fernet key shipped for local development. Refused in production.

A *valid* key, so a fresh clone runs and its whole test suite passes without
anyone generating one — and deliberately base64 of the readable ASCII string
`agentflow-dev-only-insecure-key!`, so that anybody who decodes it out of a repo
or a config dump immediately sees it is not a secret. A random-looking
placeholder would be indistinguishable from a real key at a glance, which is
exactly the wrong property for the one value nobody should ever ship.
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

    # --- Retrieval (M6) ---
    embedding_provider: Literal["openai", "hashing"] = "hashing"
    """Which `EmbeddingProvider` to build.

    Defaults to `hashing` so a fresh clone runs, and its whole test suite
    passes, with no API key and no network. That is a development convenience
    and nothing more: the hashing embedder captures *lexical* overlap, not
    meaning, so it will never match a paraphrase. The validator below refuses
    it in production, for the same reason the placeholder `SECRET_KEY` is
    refused — a default that silently degrades the product is worse than one
    that stops the process.
    """

    openai_api_key: SecretStr = SecretStr("")
    """Embeddings only — Claude is the generation model, and Anthropic has no
    embeddings endpoint. `SecretStr` so it cannot leak through a `repr()`."""

    embedding_model: str = "text-embedding-3-small"
    """OpenAI's current small model: 1536 dimensions, and cheap enough that
    re-embedding a corpus is a decision rather than a budget event."""

    embedding_dimensions: int = 1536
    """Must equal `document_chunks.embedding`'s declared width.

    A mismatch is not a graceful failure — Postgres rejects the insert, and it
    does so in the ingestion worker, long after the upload was accepted. A unit
    test asserts these agree so the failure happens at test time instead.
    """

    embedding_batch_size: int = 96
    """Texts per embedding API call. One request per chunk would turn a
    200-chunk document into 200 round trips; one request for all of them
    eventually exceeds the provider's token limit for a batch."""

    chunk_size_tokens: int = 400
    chunk_overlap_tokens: int = 60
    """Chunk geometry, in tokens rather than characters, because the limit that
    actually binds downstream is the model's context window.

    400/60 is a starting point, not a finding. M8 exists to replace these with
    numbers measured against a golden set — `docs/roadmap.md` says "we will
    tune size/overlap at M8 using eval metrics, not vibes", and until then
    these are vibes.
    """

    retrieval_top_k: int = 5
    """Default chunks returned by `/search` and fed to the model at M7. Small
    on purpose: every extra chunk is context spent, and precision at the top of
    the list matters far more than recall deep in it."""

    # --- Generation (M7) ---
    llm_provider: Literal["anthropic", "offline"] = "offline"
    """Which `LLMProvider` to build.

    Defaults to `offline` for the same reason `embedding_provider` defaults to
    `hashing`: a fresh clone must run its whole test suite with no API key and
    no network. The offline provider quotes retrieved sentences rather than
    writing prose, so it is obviously not a product — and the validator below
    refuses it in production anyway.
    """

    anthropic_api_key: SecretStr = SecretStr("")
    """`SecretStr` so it cannot leak through a `repr()`, a log line, or a
    pydantic validation error that echoes the value it failed on."""

    llm_model: str = "claude-sonnet-5"
    """Sonnet rather than Opus for RAG answers.

    The job here is to summarise three retrieved paragraphs faithfully and cite
    them, not to reason from scratch — and on that job the cheaper, faster model
    is not measurably worse. M8 exists to replace this sentence with a
    measurement; until then it is a defensible guess, and labelled as one.
    """

    llm_max_tokens: int = 1024
    """A ceiling on the *answer*, not the context.

    Required by the Anthropic API, so there is no "unlimited" to choose. 1024 is
    roughly 750 words — long enough for a thorough answer over five chunks, and
    short enough that a runaway generation is a rounding error rather than an
    incident. `Completion.was_truncated` reports when it binds, because a
    truncated answer raises nothing and reads like a complete one.
    """

    llm_temperature: float = 0.0
    """Zero, deliberately, and the one number here worth defending.

    A RAG answer is an act of faithful summarisation, not of creativity: every
    degree of sampling randomness is an opportunity to drift from the retrieved
    text, which is exactly what citations exist to prevent. Zero also makes
    answers reproducible, without which M8's evaluation would spend half its
    time measuring sampling noise instead of prompt changes.
    """

    llm_timeout_seconds: float = 60.0
    """A request with no timeout does not fail — it hangs, holding a connection
    open, and the client sees a spinner rather than an error. The same reasoning
    as `arq_job_timeout_seconds`."""

    context_token_budget: int = 8000
    """How many tokens of retrieved context may be sent with a question.

    Far below the model's context window, and that is the point. The window is
    the *limit*; this is the *budget*. Filling a 200k window with every
    plausible chunk costs real money on every question and measurably degrades
    answers — relevant passages get buried among marginal ones. Five chunks at
    400 tokens is 2k, so this leaves generous headroom while still bounding the
    bill if `top_k` is raised.
    """

    # --- Pricing and approvals (M12) ---
    llm_input_cost_per_mtok: Decimal = Decimal(0)
    llm_output_cost_per_mtok: Decimal = Decimal(0)
    """What this deployment pays per million tokens, input and output.

    **Zero by default, deliberately.** M9 refused to guess a rate because a guessed
    figure "would appear in reports, get trusted, and be wrong"; M12 owns pricing
    and keeps that refusal. The arithmetic ships; the numbers are the operator's to
    supply from their own provider's pricing page.

    A `cost_usd` of `0.000000` therefore means "nobody has told this system what it
    pays", not "this run was free" — and that is a far better thing for a report to
    say than a plausible number nobody can source.

    `Decimal`, not `float`, all the way from configuration to column. One `float` in
    the chain reintroduces exactly the rounding error `Numeric(10, 6)` was chosen to
    avoid.
    """

    approval_ttl_hours: int = 24
    """How long a pending approval stays actionable.

    See `app/models/approval.py` for why expiry is a safety property rather than
    tidiness: an approval queue with no expiry accumulates actions that get approved
    eventually, by somebody who no longer remembers why they were proposed.
    """

    # --- Conversations & memory (M10) ---
    history_token_budget: int = 2000
    """How many tokens of *conversation history* may accompany a question.

    Separate from `context_token_budget`, and that separation is the point. One
    shared budget would mean a long thread starving the retrieved documents —
    answers would get worse as a conversation got longer, with nothing in the
    logs to say why, because the total spend would look unchanged. Two budgets
    make the trade explicit and independently tunable.

    2000 is roughly a dozen turns, which covers the follow-ups a person actually
    strings together before changing subject. Beyond that the window drops the
    oldest turns, and long-term memory is what carries anything that mattered —
    see `app/agents/history.py`.
    """

    memory_top_k: int = 3
    """Long-term memories recalled per turn.

    Deliberately smaller than `retrieval_top_k`. Every memory spends prompt
    budget a *citable* document chunk could have used, and a memory is an uncited
    assertion — so it has to clear a higher bar, not a lower one.
    """

    # --- Integrations & secrets at rest (M11) ---
    token_encryption_key: SecretStr = SecretStr(PLACEHOLDER_ENCRYPTION_KEY)
    """Encrypts OAuth tokens before they are written (`core/security.py`).

    **A separate key from `SECRET_KEY`, deliberately, and it is worth defending.**
    Reusing the JWT signing key would mean one leaked value both forges sessions
    *and* decrypts every stored credential to somebody else's Google account —
    and it would couple two rotations that have completely different urgencies.
    Two keys is two things to manage; one key is one blast radius covering
    everything.

    Generate with:
    `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
    """

    oauth_provider: Literal["live", "offline"] = "offline"
    """Which `OAuthProvider` implementations to build.

    Renamed from `google` at M14, when it stopped being about Google: this one
    switch now decides whether Gmail, Google Calendar, Slack, Notion, GitHub and
    Stripe are real or in-memory. A value named after one of the six would have to
    be explained every time somebody configured Slack.

    Defaults to `offline` for the same reason `embedding_provider` and
    `llm_provider` do: a fresh clone must run its whole test suite with no
    credentials and no network. The offline provider is a complete authorization
    server that lives in memory — real codes, real expiry, real revocation, no
    Google.

    Refused in production, and the argument is sharper than for the other two. A
    bad embedder gives bad search; a bad model gives bad answers. This one would
    let a user complete a connect flow, see "Google Calendar — connected", and
    hold an integration backed by tokens that were never issued by Google and
    will never fetch a single event.
    """

    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    """Google OAuth client credentials.

    One Google Cloud OAuth client covers **both** Gmail and Google Calendar: they
    are two products of one project, differing only in the scopes requested.

    Empty by default, and *not* refused in production. A deployment that never
    connects Google is a perfectly valid deployment — unlike the placeholder
    signing key or the offline embedder, an absent integration degrades nothing.
    An unconfigured provider is simply not registered (see
    `app/integrations/__init__.py`), so asking to connect it is a 404 naming the
    two variables to set, raised where somebody is present to read it.
    """

    slack_client_id: str = ""
    slack_client_secret: SecretStr = SecretStr("")
    """Slack app credentials, from the app's Basic Information page.

    Slack calls the pair "Client ID" and "Client Secret", which is unusually
    unambiguous — its *signing* secret and its verification token are different
    strings for different features, and neither belongs here.
    """

    notion_client_id: str = ""
    notion_client_secret: SecretStr = SecretStr("")
    """Notion integration credentials, from the integration's settings page.

    Only a **public** Notion integration has these. An internal integration is
    authorized by pasting a token, has no OAuth flow at all, and cannot be
    connected by this application.
    """

    github_client_id: str = ""
    github_client_secret: SecretStr = SecretStr("")
    """GitHub OAuth App credentials.

    An *OAuth App*, not a GitHub App — they are two different products with two
    different flows, and a GitHub App's installation-based authorization does not
    fit the `OAuthProvider` seam.
    """

    stripe_client_id: str = ""
    stripe_client_secret: SecretStr = SecretStr("")
    """Stripe Connect credentials, and the pair most easily got wrong.

    `STRIPE_CLIENT_ID` is the `ca_…` **Connect application id** from the Connect
    settings page — not a publishable key, not a secret key. `STRIPE_CLIENT_SECRET`
    is the platform's own `sk_…` secret key, which authenticates the token
    exchange. Three `stripe`-looking strings, one correct assignment.
    """

    oauth_redirect_base_url: str = "http://localhost:8000"
    """The public origin each provider redirects back to.

    Configuration rather than derivation from the request, because a redirect URI
    must match Google's registered value *exactly* — and deriving it from `Host`
    would let a forwarded header change where the authorization code is sent.
    """

    oauth_state_ttl_seconds: int = 600
    """How long an in-flight connect attempt stays valid.

    Ten minutes: comfortably longer than a consent screen takes, short enough
    that an abandoned attempt cannot be resumed by someone who later finds the
    URL in a browser history or a proxy log.
    """

    # --- production hardening (M16) --------------------------------------

    rate_limit_enabled: bool = True
    """Whether `RateLimitMiddleware` counts anything.

    On by default, including in development, deliberately. A limiter that is off
    until production is a limiter first exercised in production — and the first
    thing anyone learns about it is that a legitimate workflow trips it. The
    default limit is high enough that a person cannot reach it by hand.

    The test suite turns it off, because a shared Redis counter between tests
    would make the hundredth test in a run fail for something the first test did.
    """

    rate_limit_per_minute: int = 300
    """Budget per caller per minute, where an expensive call costs five.

    300 is 60 ordinary requests a minute or 60 agent runs an hour, which no human
    reaches through a UI and a runaway script reaches in seconds. It is a guess,
    and a deliberately documented one: the number to tune once there is traffic to
    look at, and the load test in `scripts/loadtest.py` is what produces the
    figures to tune it against.
    """

    metrics_enabled: bool = True
    metrics_token: SecretStr = SecretStr("")
    """Shared secret for `GET /metrics`, and it is *not* optional in production.

    The endpoint publishes request rates, error counts, latency, token spend and
    per-agent activity. That is a free reconnaissance feed — traffic patterns,
    which routes exist, when a deploy happened, whether an attack is working — and
    it is exactly the kind of endpoint that gets left open because "it's only
    metrics".

    Empty means unauthenticated, which is right for a container scraped over a
    private network and refused below in production. A token rather than the JWT
    auth used everywhere else because Prometheus has no way to log in; a static
    bearer is what scrapers actually support.
    """

    app_version: str = "dev"
    """Which build this is, set by `docker-compose.prod.yml` from the image tag.

    Reported by `/health/live` so "which code is actually running?" is a curl
    rather than an inference from `docker ps` on a host you may not have. That
    question is the first one asked during a bad deploy and the last one anybody
    can answer from memory.

    `dev` outside a container, which is honest: a process started by `make dev` is
    whatever the working tree currently says, and no tag would describe it.
    """

    sentry_dsn: str = ""
    """Where to report unhandled exceptions, or empty to report nowhere.

    Empty by default and never required: a deployment with no Sentry account is
    valid, and failing to start over an absent error reporter would be an outage
    caused by the thing meant to observe outages.
    """

    sentry_traces_sample_rate: float = 0.0
    """Fraction of requests traced. Zero by default — performance tracing bills
    per transaction, and turning it on is a spending decision somebody should
    make on purpose rather than inherit from a default."""

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

        if self.embedding_provider == "hashing":
            # M6. The hashing embedder is offline and deterministic, which makes
            # it perfect for tests and useless for users: it matches shared
            # words, never shared meaning, so "how do I claim expenses?" would
            # miss a chunk titled "reimbursement policy".
            #
            # Nothing would *break*. Search would simply be quietly bad, which
            # is the failure mode a startup check is worth having for.
            message = (
                "EMBEDDING_PROVIDER is 'hashing' in production. That embedder "
                "matches words, not meaning, and exists for offline development. "
                "Set EMBEDDING_PROVIDER=openai and provide OPENAI_API_KEY."
            )
            raise ValueError(message)

        if self.embedding_provider == "openai" and not self.openai_api_key.get_secret_value():
            # Caught here rather than on the first upload. Without this the
            # process starts, accepts documents, and fails inside the worker —
            # so the user sees `status=failed` on their file rather than an
            # operator seeing a misconfigured deploy.
            message = "EMBEDDING_PROVIDER is 'openai' but OPENAI_API_KEY is empty."
            raise ValueError(message)

        if self.llm_provider == "offline":
            # M7, and the same argument as the embedder one above with the
            # volume turned up. The offline provider does not generate — it
            # quotes the best-matching retrieved sentence. Shipped to users it
            # would answer every question with a fragment of a document and no
            # explanation, which is not a degraded product so much as a
            # different, worse one.
            message = (
                "LLM_PROVIDER is 'offline' in production. That provider quotes "
                "retrieved sentences instead of generating answers, and exists "
                "for offline development. Set LLM_PROVIDER=anthropic and "
                "provide ANTHROPIC_API_KEY."
            )
            raise ValueError(message)

        if not self.anthropic_api_key.get_secret_value():
            # Caught at startup rather than on the first question. Without this
            # the process starts healthy, passes its readiness probe, and fails
            # only when a user asks something — so the deploy looks fine and the
            # feature is simply broken.
            message = "LLM_PROVIDER is 'anthropic' but ANTHROPIC_API_KEY is empty."
            raise ValueError(message)

        if self.oauth_provider == "offline":
            # M11. The offline provider mints tokens no provider ever issued, so
            # a connect flow would appear to succeed and produce an integration
            # that can never fetch anything — a feature that looks connected and
            # is not.
            message = (
                "OAUTH_PROVIDER is 'offline' in production. That provider is an "
                "in-memory authorization server for development; it issues tokens "
                "no provider ever granted. Set OAUTH_PROVIDER=live."
            )
            raise ValueError(message)

        if self.metrics_enabled and not self.metrics_token.get_secret_value():
            # M16. `/metrics` publishes traffic rates, error counts, latency and
            # spend — a free reconnaissance feed for anyone who finds it, and the
            # canonical endpoint people forget to protect because "it's only
            # metrics". Refused rather than defaulted to off: silently disabling
            # monitoring in production is the other way to lose.
            message = (
                "METRICS_TOKEN is empty in production. /metrics publishes traffic "
                "and spend; set a token, or set METRICS_ENABLED=false if the "
                "endpoint is unreachable from outside the cluster."
            )
            raise ValueError(message)

        if self.token_encryption_key.get_secret_value() == PLACEHOLDER_ENCRYPTION_KEY:
            # M11, and it belongs beside the SECRET_KEY check because it guards
            # the same class of disaster from the other direction. That key
            # forges *our* sessions; this one decrypts stored credentials to
            # *other people's* Google accounts — a compromise we would have to
            # disclose, and one nobody could remediate by rotating a password.
            #
            # The placeholder is published in this repository, so shipping it is
            # equivalent to storing the tokens in plaintext with extra steps.
            message = (
                "TOKEN_ENCRYPTION_KEY is still the development placeholder in "
                "production. That key is published in this repository, so OAuth "
                "tokens would effectively be stored in plaintext. Generate one "
                'with: python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
            raise ValueError(message)

        if self.token_encryption_key == self.secret_key:
            # Two keys, two blast radii. Sharing them means one leak both mints
            # tokens for any user and decrypts every stored credential, and it
            # couples two rotations whose urgencies have nothing in common.
            message = (
                "TOKEN_ENCRYPTION_KEY must not equal SECRET_KEY. One signs our "
                "own sessions; the other protects credentials to somebody else's "
                "account, and sharing them makes a single leak cost both."
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
