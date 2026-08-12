"""Password hashing and JWT primitives (M3).

Security code is where "it seems to work" is least trustworthy: a broken
implementation authenticates the happy path perfectly and fails only against
someone deliberately attacking it. So most of what follows tests the attacks —
tampering, algorithm confusion, token-type confusion, expiry.
"""

from __future__ import annotations

import time
from datetime import timedelta

import jwt
import pytest
from pydantic import ValidationError

from app.core.config import MINIMUM_SECRET_BYTES, PLACEHOLDER_SECRET, Settings
from app.core.exceptions import AuthenticationError
from app.core.security import (
    create_token,
    decode_token,
    hash_password,
    needs_rehash,
    verify_password,
)

PASSWORD = "correct horse battery staple"


# --------------------------------------------------------------------------
# Password hashing
# --------------------------------------------------------------------------


def test_hash_is_argon2id_and_not_the_password() -> None:
    """The obvious property, stated so a refactor cannot quietly break it."""
    hashed = hash_password(PASSWORD)

    assert hashed.startswith("$argon2id$")
    assert PASSWORD not in hashed


def test_the_same_password_hashes_differently_every_time() -> None:
    """Per-hash salt.

    Without it, identical passwords produce identical hashes, so one glance at
    a leaked table tells an attacker which accounts share a password — and one
    precomputed rainbow table cracks all of them at once.
    """
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_verify_accepts_the_right_password_and_rejects_others() -> None:
    hashed = hash_password(PASSWORD)

    assert verify_password(PASSWORD, hashed) is True
    assert verify_password("wrong password", hashed) is False
    assert verify_password(PASSWORD.upper(), hashed) is False


def test_verify_returns_false_on_a_corrupt_hash_instead_of_raising() -> None:
    """A mangled column value must read as "wrong password", not as a 500.

    Callers treat the bool as "should this login succeed?". An exception
    escaping here would turn a data problem into an outage.
    """
    assert verify_password(PASSWORD, "not-a-hash") is False
    assert verify_password(PASSWORD, "") is False


def test_current_hashes_do_not_need_rehashing() -> None:
    assert needs_rehash(hash_password(PASSWORD)) is False


def test_a_weaker_hash_is_flagged_for_rehash() -> None:
    """Cost parameters rise over time; login is the only chance to upgrade one."""
    weak = "$argon2id$v=19$m=8,t=1,p=1$c29tZXNhbHQ$" + "a" * 43

    assert needs_rehash(weak) is True


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------


def test_token_carries_exactly_the_expected_claims() -> None:
    """Pinned because claims are a wire contract — and because of what is absent.

    No email, no role, no organization: anything embedded in a token keeps
    being true to whoever reads it long after it stopped being true.
    """
    token = create_token("user-123", token_type="access", expires_in=timedelta(minutes=5))

    claims = decode_token(token, expected_type="access")

    assert set(claims) == {"sub", "iat", "exp", "jti", "typ"}
    assert claims["sub"] == "user-123"
    assert claims["typ"] == "access"


def test_every_token_gets_a_unique_jti() -> None:
    """The jti is the handle the denylist revokes; duplicates would revoke in pairs."""
    first = decode_token(
        create_token("u", token_type="refresh", expires_in=timedelta(days=1)),
        expected_type="refresh",
    )
    second = decode_token(
        create_token("u", token_type="refresh", expires_in=timedelta(days=1)),
        expected_type="refresh",
    )

    assert first["jti"] != second["jti"]


def test_an_expired_token_is_rejected() -> None:
    token = create_token("u", token_type="access", expires_in=timedelta(seconds=-1))

    with pytest.raises(AuthenticationError):
        decode_token(token, expected_type="access")


def test_a_tampered_payload_is_rejected() -> None:
    """The entire point of a signature.

    A JWT is signed, not encrypted: anyone can read the payload and edit it.
    Only the signature stops the edit from being believed.
    """
    token = create_token("user-123", token_type="access", expires_in=timedelta(minutes=5))
    header, payload, signature = token.split(".")
    forged = f"{header}.{payload[:-4]}AAAA.{signature}"

    with pytest.raises(AuthenticationError):
        decode_token(forged, expected_type="access")


def test_a_token_signed_with_another_key_is_rejected() -> None:
    """Someone else's valid-looking token is still someone else's."""
    foreign = jwt.encode(
        {
            "sub": "attacker",
            "iat": time.time(),
            "exp": time.time() + 300,
            "jti": "x",
            "typ": "access",
        },
        "a-completely-different-signing-key-of-sufficient-length",
        algorithm="HS256",
    )

    with pytest.raises(AuthenticationError):
        decode_token(foreign, expected_type="access")


def test_an_unsigned_token_is_rejected() -> None:
    """`alg: none` — the textbook JWT attack.

    A library that trusts the token's own header will happily accept a token
    carrying no signature at all. `algorithms=[...]` is a whitelist, which is
    why this fails.
    """
    unsigned = jwt.encode(
        {
            "sub": "attacker",
            "iat": time.time(),
            "exp": time.time() + 300,
            "jti": "x",
            "typ": "access",
        },
        key="",
        algorithm="none",
    )

    with pytest.raises(AuthenticationError):
        decode_token(unsigned, expected_type="access")


def test_an_access_token_cannot_be_used_as_a_refresh_token() -> None:
    """Token-type confusion.

    Without the `typ` check, a stolen 30-minute access token could be posted to
    /auth/refresh to mint a fresh pair — upgrading a short-lived credential
    into an indefinite one.
    """
    access = create_token("u", token_type="access", expires_in=timedelta(minutes=5))

    with pytest.raises(AuthenticationError):
        decode_token(access, expected_type="refresh")


def test_a_refresh_token_cannot_be_used_as_an_access_token() -> None:
    """The other direction: a 7-day token must not open protected routes."""
    refresh = create_token("u", token_type="refresh", expires_in=timedelta(days=7))

    with pytest.raises(AuthenticationError):
        decode_token(refresh, expected_type="access")


def test_garbage_is_rejected_without_leaking_why() -> None:
    """One message for every failure mode — the client's next move is the same."""
    for rubbish in ("", "not.a.token", "a.b.c"):
        with pytest.raises(AuthenticationError) as error:
            decode_token(rubbish, expected_type="access")

        assert str(error.value) == "Could not validate credentials"


# --------------------------------------------------------------------------
# Startup guards
# --------------------------------------------------------------------------


def test_production_refuses_the_placeholder_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """A known signing key means anyone can mint a token for any user."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", PLACEHOLDER_SECRET)

    with pytest.raises(ValidationError, match="placeholder"):
        Settings()


def test_production_refuses_a_short_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """RFC 7518 §3.2 — a short HMAC key silently shrinks the brute-force space."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "a" * (MINIMUM_SECRET_BYTES - 1))

    with pytest.raises(ValidationError, match="at least 32 bytes"):
        Settings()


def test_production_accepts_a_real_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "f" * 64)

    assert Settings().secret_key.get_secret_value() == "f" * 64


def test_development_tolerates_the_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    """`make dev` on a fresh clone must work without ceremony."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SECRET_KEY", PLACEHOLDER_SECRET)

    assert Settings().env == "development"


def test_the_secret_does_not_appear_in_a_repr() -> None:
    """SecretStr. Settings objects get printed by loggers and validation errors."""
    assert PLACEHOLDER_SECRET not in repr(Settings())
