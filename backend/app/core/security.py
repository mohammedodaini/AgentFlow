"""Security primitives — hashing, JWT encode/decode, secret encryption.

Layer: core (leaf). Pure functions only; no DB, no request context.
The auth *feature* (flows, dependencies) lives in app/auth/ and uses these.

The split is deliberate. These functions are the parts that must be boring and
verifiable: given this input, exactly this output, no I/O to mock. Policy —
how long a session lasts, when a refresh token rotates, who may do what —
lives one layer up, where it can change without anyone touching cryptography.
"""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError

TokenType = Literal["access", "refresh"]
"""Written into the `typ` claim and checked on the way back in.

Without this claim the two token types are interchangeable, and a stolen
30-minute access token could be presented to `/auth/refresh` to mint a fresh
pair — turning a short-lived credential into a permanent one. The check in
`decode_token` is what closes that.
"""

_password_hasher = PasswordHasher()
"""Argon2id with argon2-cffi's current defaults (RFC 9106's second recommended
option: 64 MiB, 3 passes, 4 lanes).

Argon2id rather than bcrypt or PBKDF2 because it is *memory*-hard: a GPU or
ASIC gets far less advantage over an ordinary CPU, which is the entire
economics of offline cracking. It won the Password Hashing Competition and is
OWASP's first recommendation.

Deliberately not tuned by hand. Defaults track the library, whereas a number
someone picked once in 2026 will still be sitting here, unchanged, in 2031.
"""


MAX_HASHING_THREADS = 4
"""How many passwords may be hashed at once, and **memory is what sets it.**

Argon2id is *memory*-hard — that is the property that defeats GPUs — and
argon2-cffi's default `memory_cost` is 65536 KiB, so **each concurrent hash
allocates 64 MiB**. Four is 256 MiB of peak RSS, which is survivable on a small
container. `asyncio.to_thread`'s default executor is sized `min(32, cpu_count+4)`,
and thirty-two concurrent hashes is **2 GB** — an OOM kill on any container with a
1 GB limit, triggered by unauthenticated traffic.

Not sized from `os.cpu_count()`, which was the first attempt and is wrong for this
deployment specifically: inside a container that returns the *host's* core count,
not the cgroup quota. A 2-core container on a 64-core host would build a 64-thread
pool and reserve 4 GB it does not have.

Four is ample. At ~37ms a hash that is ~108 sign-ins a second, far past anything
this application needs, and work beyond it queues rather than failing.
"""

_HASHING_POOL = ThreadPoolExecutor(max_workers=MAX_HASHING_THREADS, thread_name_prefix="argon2")
"""Where password hashing actually runs, off the event loop.

Argon2 is expensive by design — ~37ms of CPU per verification on the machine this
was written on — and calling it from an `async` path runs it **on the event
loop**, blocking every other in-flight request for that whole time. Measured
before the fix: 72 sign-in attempts took `/health/live` from a 3.2ms median to
60.7ms, and no account was needed, because the timing equaliser in
`AuthService.login` hashes for unknown emails too.

**Be clear about what this does and does not fix.** It removes head-of-line
blocking: one sign-in can no longer stall unrelated requests, which the tests in
`tests/e2e/test_auth.py` assert directly. It does **not** stop a flood from
exhausting the machine — 400 hashes is 14.7 seconds of CPU wherever it runs, and
moving CPU work between threads cannot make it cheaper. Measured with rate
limiting disabled, the median was 496ms before this change and 521ms after it.

The defence against the flood is **refusing** it, which is why `/api/v1/auth` is
in `EXPENSIVE_PREFIXES` (`app/middleware/rate_limit.py`). With the limiter on, the
same 400 attempts left `/health/ready` at a 8.9ms median. This pool bounds the
*memory* and keeps the loop responsive; the limiter bounds the work.

A `ThreadPoolExecutor` rather than an `asyncio.Semaphore`: asyncio primitives bind
to the loop that first awaits them, and this process creates a fresh loop per
test. An executor is loop-agnostic and thread-safe, so it cannot acquire a subtle
lifetime bug.
"""


async def verify_password_async(plain_password: str, password_hash: str) -> bool:
    """`verify_password`, off the event loop and inside the bounded pool.

    Every caller in an async context must use this. The synchronous version stays
    for tests and for `needs_rehash` comparisons, where no loop is involved.
    """
    return await asyncio.get_running_loop().run_in_executor(
        _HASHING_POOL, verify_password, plain_password, password_hash
    )


async def hash_password_async(plain_password: str) -> str:
    """`hash_password`, off the event loop and inside the bounded pool."""
    return await asyncio.get_running_loop().run_in_executor(
        _HASHING_POOL, hash_password, plain_password
    )


def hash_password(plain_password: str) -> str:
    """Return an Argon2id hash, salt and parameters included.

    The returned string carries its own salt and cost parameters, e.g.
    `$argon2id$v=19$m=65536,t=3,p=4$...`. That is why there is no separate salt
    column: the verifier reads the parameters back out of the stored hash,
    which is also what makes `needs_rehash` possible.
    """
    return _password_hasher.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Return whether the password matches, without raising on mismatch.

    A bool rather than an exception because the caller must treat "wrong
    password" and "no such user" identically — see `AuthService.login`.
    """
    try:
        return _password_hasher.verify(password_hash, plain_password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Whether this hash used weaker parameters than we now use.

    Cost parameters should rise as hardware gets faster. Login is the only
    moment the plaintext exists, so it is the only moment a stored hash can be
    upgraded — every user re-hashes silently, the next time they sign in.
    """
    return _password_hasher.check_needs_rehash(password_hash)


def create_token(subject: str, *, token_type: TokenType, expires_in: timedelta) -> str:
    """Mint a signed JWT.

    Claims are the registered ones plus `typ`:

    * `sub` — who the token is about (the user id as a string; the spec
      requires a string, and a UUID object would not survive the round trip)
    * `exp` / `iat` — expiry and issue time, both UTC
    * `jti` — a unique token id. This is what makes a refresh token
      *revocable*: the denylist stores jtis, not whole tokens.
    * `typ` — "access" or "refresh"

    Note what is *not* in here: no email, no role, no organization. Anything
    embedded in a token is a snapshot that keeps being true to the reader long
    after it stopped being true — demote an admin and their existing token
    still says "admin" until it expires. Roles are read from the database on
    each request instead.
    """
    settings = get_settings()
    now = datetime.now(UTC)

    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + expires_in,
        "jti": str(uuid.uuid4()),
        "typ": token_type,
    }

    return jwt.encode(payload, settings.secret_key.get_secret_value(), settings.jwt_algorithm)


def decode_token(token: str, *, expected_type: TokenType) -> dict[str, Any]:
    """Verify a token's signature, expiry and type; return its claims.

    Raises `AuthenticationError` for every failure mode — bad signature,
    expired, malformed, wrong type. The caller does not get to distinguish
    them, and should not: telling an attacker *why* a token was rejected is
    free information, and the correct client behaviour ("get a new token") is
    identical in every case.
    """
    settings = get_settings()

    try:
        payload = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            # `algorithms` is a whitelist, not a hint. Trusting the token's own
            # `alg` header is the classic JWT vulnerability: an attacker sets
            # alg=none, or swaps RS256 for HS256 and signs with the public key.
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "iat", "sub", "jti", "typ"]},
        )
    except jwt.PyJWTError as exc:
        message = "Could not validate credentials"
        raise AuthenticationError(message) from exc

    if payload.get("typ") != expected_type:
        message = "Could not validate credentials"
        raise AuthenticationError(message)

    return payload


def _fernet() -> Fernet:
    """The cipher for secrets at rest, built per call from settings.

    Not a module-level constant, because `get_settings()` is cached and tests
    monkeypatch the environment — a cipher captured at import time would keep
    using the key from whichever test ran first.
    """
    return Fernet(get_settings().token_encryption_key.get_secret_value().encode())


def encrypt_secret(value: str) -> str:
    """Encrypt a credential for storage.

    Fernet: AES-128-CBC with an HMAC-SHA256 signature over the ciphertext, and a
    random IV per call. Authenticated, so a tampered value fails to decrypt
    rather than decrypting to something else — which matters here, because the
    plaintext is a bearer credential and "decrypts to garbage" would be handed
    straight to Google as though it were real.

    **Non-deterministic, and that is a constraint on the schema rather than a
    detail.** Encrypting the same token twice gives different ciphertext, so an
    encrypted column can never be queried, indexed, or made UNIQUE.
    `oauth_tokens` is therefore always reached through `integration_id`, never
    through a token value — and anything that ever needs to look one up by
    content needs a separate deterministic keyed hash and its own decision.
    """
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    """Recover a stored credential, or raise if it cannot be trusted.

    Raises `AuthenticationError` rather than propagating `InvalidToken`, for the
    same reason `decode_token` does: the caller's correct response is identical
    whether the key rotated, the row was tampered with, or the ciphertext was
    truncated by a bad migration — the credential is unusable and the
    integration has to be reconnected.

    Rotating the key is the case this signature deliberately leaves room for.
    `MultiFernet` decrypts with any key in a list while encrypting with the
    first, which is what makes a rotation a deploy rather than an outage. It is
    not wired up: one key is honest today, and doing otherwise would mean
    shipping a rotation path nothing has ever exercised.
    """
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as error:
        message = "Stored credential could not be decrypted."
        raise AuthenticationError(message) from error
