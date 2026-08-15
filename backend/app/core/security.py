"""Security primitives — hashing, JWT encode/decode, secret encryption.

Layer: core (leaf). Pure functions only; no DB, no request context.
The auth *feature* (flows, dependencies) lives in app/auth/ and uses these.

The split is deliberate. These functions are the parts that must be boring and
verifiable: given this input, exactly this output, no I/O to mock. Policy —
how long a session lasts, when a refresh token rotates, who may do what —
lives one layer up, where it can change without anyone touching cryptography.
"""

from __future__ import annotations

import uuid
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
