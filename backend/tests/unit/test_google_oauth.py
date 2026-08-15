"""Google's token endpoint, and telling a dead credential from a bad day (M11).

The classification in `_token_request` is the most consequential logic in the
integration and the least reachable without a seam — exercising it for real means
persuading Google to reject a credential. So it is driven through a
`MockTransport` with the payloads Google actually sends.

Getting it wrong is expensive in both directions. Treat `invalid_grant` as
retryable and a background job hammers a credential that can never work again,
while the user sees an integration claiming to be connected. Treat a 500 as
permanent and one blip disconnects a working account and makes somebody redo a
consent flow.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.integrations.base import OAuthError, OAuthRevokedError
from app.integrations.google_calendar.oauth import TOKEN_URL, USERINFO_URL, GoogleCalendarOAuth

REDIRECT = "https://app.example.test/api/v1/integrations/google_calendar/callback"

TOKEN_RESPONSE = {
    "access_token": "ya29.a0-access",
    "refresh_token": "1//0g-refresh",
    "expires_in": 3599,
    "scope": "https://www.googleapis.com/auth/calendar.readonly",
    "token_type": "Bearer",
}


def provider_with(handler: Callable[[httpx.Request], httpx.Response]) -> GoogleCalendarOAuth:
    return GoogleCalendarOAuth(Settings(), transport=httpx.MockTransport(handler))


def routed(
    *, token: dict[str, Any], token_status: int = 200, email: str | None = "ada@example.test"
) -> GoogleCalendarOAuth:
    """A provider whose token and userinfo endpoints return canned payloads."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(TOKEN_URL):
            return httpx.Response(token_status, json=token)

        if str(request.url).startswith(USERINFO_URL):
            if email is None:
                return httpx.Response(500, text="upstream trouble")

            return httpx.Response(200, json={"email": email})

        raise AssertionError(f"unexpected request to {request.url}")

    return provider_with(handler)


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


async def test_a_code_becomes_a_grant() -> None:
    grant = await routed(token=TOKEN_RESPONSE).exchange_code(code="c", redirect_uri=REDIRECT)

    assert grant.access_token == "ya29.a0-access"
    assert grant.refresh_token == "1//0g-refresh"
    assert grant.expires_at is not None
    assert grant.scopes == ["https://www.googleapis.com/auth/calendar.readonly"]
    assert grant.external_account_id == "ada@example.test"


async def test_the_account_label_is_optional() -> None:
    """Failure to fetch the email is deliberately not fatal. The address is for
    humans to read, and a connection that works but cannot label itself beats
    discarding an authorization the user just completed because a secondary lookup
    timed out."""
    grant = await routed(token=TOKEN_RESPONSE, email=None).exchange_code(
        code="c", redirect_uri=REDIRECT
    )

    assert grant.access_token == "ya29.a0-access"
    assert grant.external_account_id is None


async def test_refreshing_reports_the_absent_refresh_token_faithfully() -> None:
    """Google sends no `refresh_token` on a refresh, and this method does not
    helpfully substitute the input.

    Substituting would hide the bug from every test: a caller that overwrites its
    stored refresh token blindly would then appear to work, and the integration
    would die an hour after the next refresh. Reporting the truth is what lets
    `IntegrationService` be tested for keeping it.
    """
    response = {"access_token": "ya29.a0-new", "expires_in": 3599, "scope": "calendar.readonly"}

    grant = await routed(token=response).refresh("1//0g-refresh")

    assert grant.access_token == "ya29.a0-new"
    assert grant.refresh_token is None


# --------------------------------------------------------------------------
# permanent failures
# --------------------------------------------------------------------------


async def test_invalid_grant_is_permanent() -> None:
    """Google's answer for a refresh token revoked from the account page, expired
    after six months of disuse, or invalidated by a password change. All normal
    events on the user's side, and none of them retryable."""
    with pytest.raises(OAuthRevokedError):
        await routed(token={"error": "invalid_grant"}, token_status=400).refresh("dead")


async def test_the_body_decides_rather_than_the_status_code() -> None:
    """`invalid_grant` arrives with HTTP **400**, not 401 — so a classifier that
    only read the status would file a dead credential under "bad request" and
    retry it forever."""
    with pytest.raises(OAuthRevokedError):
        await routed(token={"error": "invalid_token"}, token_status=400).refresh("dead")


# --------------------------------------------------------------------------
# transient failures
# --------------------------------------------------------------------------


async def test_a_server_error_is_retryable() -> None:
    """Not a revocation. Disconnecting a working account over a blip would make
    somebody redo a consent flow for nothing."""
    with pytest.raises(OAuthError) as caught:
        await routed(token={"error": "backend_error"}, token_status=500).refresh("live")

    assert not isinstance(caught.value, OAuthRevokedError)


async def test_a_network_failure_is_translated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        message = "connection refused"
        raise httpx.ConnectError(message)

    with pytest.raises(OAuthError, match="token endpoint"):
        await provider_with(handler).refresh("live")


async def test_an_html_error_page_does_not_crash_the_parser() -> None:
    """Google's load balancers return HTML during an incident. A
    `JSONDecodeError` escaping here would surface as a 500 mentioning neither
    Google nor OAuth."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(502, text="<html><body>Error 502</body></html>")

    with pytest.raises(OAuthError):
        await provider_with(handler).refresh("live")


async def test_a_200_with_no_token_is_an_error() -> None:
    """Raised here rather than allowed to surface as a `KeyError` three frames
    later, where the traceback would name a dataclass instead of the provider that
    misbehaved."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"token_type": "Bearer"})

    with pytest.raises(OAuthError, match="no access_token"):
        await provider_with(handler).refresh("live")
