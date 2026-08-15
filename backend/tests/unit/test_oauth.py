"""Encryption at rest, and the OAuth provider seam (M11).

Three things are pinned here, and each of them fails silently in production if it
drifts:

- **Ciphertext is authenticated and non-deterministic.** The first property means
  a tampered row raises instead of decrypting to something plausible; the second
  is what makes an encrypted column unindexable, which is a constraint the schema
  depends on.
- **The offline authorization server behaves like a real one.** Single-use codes,
  and no new refresh token on refresh. Both are modelled deliberately, because
  code that gets them wrong passes every test written against a friendlier double.
- **Google's authorize URL carries the two parameters that decide whether a
  refresh token exists at all.** Omit either and the integration works for an
  hour, which is long enough to ship.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError
from app.core.security import decrypt_secret, encrypt_secret
from app.integrations.base import OAuthProvider, OAuthRevokedError, TokenGrant
from app.integrations.google_calendar.oauth import SCOPES, GoogleCalendarOAuth
from app.integrations.offline import CODE_PREFIX, OfflineOAuthProvider

REDIRECT = "https://app.example.test/api/v1/integrations/google_calendar/callback"


def offline() -> OfflineOAuthProvider:
    return OfflineOAuthProvider("google_calendar", ["calendar.readonly"])


def code_from(url: str) -> str:
    """The authorization code the offline server embedded in its redirect."""
    return parse_qs(urlparse(url).query)["code"][0]


# --------------------------------------------------------------------------
# encryption at rest
# --------------------------------------------------------------------------


def test_a_credential_survives_a_round_trip() -> None:
    assert decrypt_secret(encrypt_secret("ya29.a0-refresh-token")) == "ya29.a0-refresh-token"


def test_ciphertext_never_repeats() -> None:
    """Fernet uses a random IV per call, and that is a *schema* constraint rather
    than a detail: an encrypted column can never be indexed, compared or made
    UNIQUE, so a token is only ever reached through its integration id."""
    assert encrypt_secret("same") != encrypt_secret("same")


def test_the_plaintext_never_appears_in_the_ciphertext() -> None:
    """The point of the exercise. A database dump, a backup, or a `SELECT *` in a
    support session must not yield a usable credential."""
    assert "refresh-token" not in encrypt_secret("ya29.a0-refresh-token")


def test_a_tampered_value_is_rejected_rather_than_mangled() -> None:
    """Fernet authenticates the ciphertext, so a modified row fails loudly.

    Without authentication a flipped byte would decrypt to *something* — and that
    something would be sent to Google as a bearer credential.
    """
    ciphertext = encrypt_secret("ya29.a0-refresh-token")
    tampered = ciphertext[:-4] + ("AAAA" if not ciphertext.endswith("AAAA") else "BBBB")

    with pytest.raises(AuthenticationError):
        decrypt_secret(tampered)


def test_a_value_from_another_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """What a key rotation looks like before `MultiFernet` is wired up: the old
    ciphertext is unreadable and the integration must be reconnected. Raising is
    the honest outcome — the alternative is returning a token that is not one."""
    ciphertext = encrypt_secret("ya29.a0-refresh-token")

    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()

    try:
        with pytest.raises(AuthenticationError):
            decrypt_secret(ciphertext)
    finally:
        # The cache is process-wide, so leaving a rotated key in it would make
        # every later test decrypt with a key nothing was encrypted under.
        get_settings.cache_clear()


def test_the_encryption_key_is_not_the_signing_key() -> None:
    """Two keys, two blast radii. Sharing them means one leak both forges our
    sessions and decrypts credentials to somebody else's Google account."""
    settings = Settings()

    assert (
        settings.token_encryption_key.get_secret_value() != settings.secret_key.get_secret_value()
    )


# --------------------------------------------------------------------------
# the token grant
# --------------------------------------------------------------------------


def test_expiry_is_stored_absolutely() -> None:
    """Converted from the provider's relative `expires_in` at receipt. Keeping the
    relative value would make every later check depend on knowing exactly when the
    response arrived — a fact nobody records."""
    grant = TokenGrant.from_response({"access_token": "a", "expires_in": 3600})

    assert grant.expires_at is not None
    assert grant.expires_at > datetime.now(UTC)


def test_scopes_come_from_the_response() -> None:
    """A user can untick a permission on the consent screen and Google returns the
    reduced set without complaint. Recording our own *request* would mean
    believing we have access we do not."""
    grant = TokenGrant.from_response({"access_token": "a", "scope": "calendar.readonly email"})

    assert grant.scopes == ["calendar.readonly", "email"]


def test_a_response_without_an_expiry_is_not_invented() -> None:
    """None, and `needs_refresh` treats it as expired. Guessing a lifetime would
    mean confidently using a token that is already dead."""
    assert TokenGrant.from_response({"access_token": "a"}).expires_at is None


# --------------------------------------------------------------------------
# the offline authorization server
# --------------------------------------------------------------------------


def test_the_offline_provider_satisfies_the_protocol() -> None:
    """Asserted here rather than discovered as an `AttributeError` during a
    callback somebody is watching."""
    assert isinstance(offline(), OAuthProvider)


def test_the_real_provider_satisfies_the_protocol() -> None:
    assert isinstance(GoogleCalendarOAuth(Settings()), OAuthProvider)


async def test_a_code_can_be_exchanged_once() -> None:
    """A callback URL sits in browser history, in a proxy log and in a `Referer`
    header. An authorization server that honoured a code twice would let anyone
    who found one mint a credential."""
    provider = offline()
    code = code_from(provider.authorize_url(state="s", redirect_uri=REDIRECT))

    grant = await provider.exchange_code(code=code, redirect_uri=REDIRECT)
    assert grant.access_token

    with pytest.raises(OAuthRevokedError):
        await provider.exchange_code(code=code, redirect_uri=REDIRECT)


async def test_an_unknown_code_is_refused() -> None:
    with pytest.raises(OAuthRevokedError):
        await offline().exchange_code(code=f"{CODE_PREFIX}invented", redirect_uri=REDIRECT)


async def test_refreshing_returns_no_new_refresh_token() -> None:
    """Exactly what Google does, and the single most common way a token store
    corrupts itself: code that writes the whole grant back overwrites a long-lived
    refresh token with NULL, and everything works until the access token expires
    an hour later.

    Making this the *default* behaviour of the double means any caller that gets
    it wrong fails immediately rather than in an hour.
    """
    provider = offline()
    code = code_from(provider.authorize_url(state="s", redirect_uri=REDIRECT))
    first = await provider.exchange_code(code=code, redirect_uri=REDIRECT)

    assert first.refresh_token is not None
    refreshed = await provider.refresh(first.refresh_token)

    assert refreshed.access_token != first.access_token
    assert refreshed.refresh_token is None


async def test_a_revoked_credential_stays_revoked() -> None:
    """Revocation is a *state*, not one failed request — which is why the double is
    stateful rather than a mocked HTTP call. Every later attempt must reach the
    same conclusion."""
    provider = offline()
    code = code_from(provider.authorize_url(state="s", redirect_uri=REDIRECT))
    grant = await provider.exchange_code(code=code, redirect_uri=REDIRECT)
    assert grant.refresh_token is not None

    provider.revoke(grant.refresh_token)

    for _ in range(2):
        with pytest.raises(OAuthRevokedError):
            await provider.refresh(grant.refresh_token)


def test_the_offline_authorize_url_can_never_reach_a_real_host() -> None:
    """`.test` is reserved by RFC 2606 and resolves nowhere. If this URL ever
    escaped into a deployment, the browser would fail loudly instead of quietly
    delivering an authorization request to somebody's server."""
    url = offline().authorize_url(state="s", redirect_uri=REDIRECT)

    assert urlparse(url).hostname == "offline.agentflow.test"


# --------------------------------------------------------------------------
# Google's authorize URL
# --------------------------------------------------------------------------


def test_the_authorize_url_asks_for_offline_access() -> None:
    """`access_type=offline` is what produces a refresh token at all. Without it
    Google issues a one-hour access token and nothing else, and the integration
    works beautifully until lunchtime."""
    url = GoogleCalendarOAuth(Settings()).authorize_url(state="s", redirect_uri=REDIRECT)

    assert parse_qs(urlparse(url).query)["access_type"] == ["offline"]


def test_the_authorize_url_forces_consent() -> None:
    """`prompt=consent` is what produces a refresh token on a *re*-connect. Google
    only issues one the first time a scope set is granted, so without this the
    reconnection meant to fix a broken integration breaks it again — an hour
    later."""
    url = GoogleCalendarOAuth(Settings()).authorize_url(state="s", redirect_uri=REDIRECT)

    assert parse_qs(urlparse(url).query)["prompt"] == ["consent"]


def test_the_authorize_url_carries_the_state_and_redirect() -> None:
    url = GoogleCalendarOAuth(Settings()).authorize_url(state="abc123", redirect_uri=REDIRECT)
    query = parse_qs(urlparse(url).query)

    assert query["state"] == ["abc123"]
    assert query["redirect_uri"] == [REDIRECT]


def test_only_read_access_to_the_calendar_is_requested() -> None:
    """M11's scope boundary. A write scope requested now would sit unused until
    M12 while every connected user had already granted an agent permission to
    alter their diary."""
    assert any(scope.endswith("calendar.readonly") for scope in SCOPES)
    assert not any(scope.rstrip("/").endswith("auth/calendar") for scope in SCOPES)
