"""The Google OAuth flow, shared by every Google product this app connects.

Layer: integrations. Implements `OAuthProvider`; stores nothing.

Why this module exists at M14 and did not at M11
------------------------------------------------
M11 put all of this inside `google_calendar/oauth.py`, which was correct: one
Google product, and a shared base class abstracting a single case is a guess
about a future that may never arrive.

M14 connects Gmail, and the future arrived. Everything below — `access_type`,
`prompt`, the `invalid_grant`-with-HTTP-400 classification, the userinfo lookup —
is identical for the two, and the *only* differences are the scope list and the
provider name. Copying two hundred lines to change two would mean the next fix to
Google's error handling lands in one file and not the other, and the symptom
would be Calendar recovering from a revoked credential while Gmail retries it
forever.

So `GoogleCalendarOAuth` and `GmailOAuth` are now four-line subclasses. The
scopes stay in their own product's module, because a scope list is the thing a
reviewer must be able to find without knowing this file exists.

Three Google-specific facts this module encodes
------------------------------------------------
**`access_type=offline` is what produces a refresh token at all.** Without it
Google issues a one-hour access token and nothing else, and the integration works
beautifully until lunchtime. It is a query parameter on the *authorize* URL — not
the token exchange — so getting it wrong is invisible until the first expiry.

**`prompt=consent` is what produces one on a *re*-connect.** Google only returns a
refresh token the first time a user grants a given scope set. Someone who
disconnects and reconnects would otherwise get an access token and a NULL refresh
token, and the reconnection meant to fix their integration would break it in a way
that takes an hour to appear.

**`invalid_grant` is the answer for a dead credential**, and it arrives with HTTP
400 rather than 401 — so the status code alone cannot classify it. The body has to
be read, which is why `_token_request` inspects `error` before anything else.
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from app.core.config import Settings
from app.integrations.base import (
    DEFAULT_TIMEOUT_SECONDS,
    OAuthError,
    OAuthRevokedError,
    TokenGrant,
)

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — a URL, not a secret
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
"""Requested by every Google product here.

It answers "which account is this?" — without it a user with a personal and a
work Google account cannot tell which one they connected, or that they connected
the wrong one.
"""

REVOKED_ERRORS = {"invalid_grant", "invalid_token"}
"""Google's vocabulary for "this credential is dead".

`invalid_grant` covers a refresh token revoked from the account page, expired
after six months of disuse, or invalidated by a password change. All three are
normal events on the user's side, and none of them is retryable.
"""


class GoogleOAuth:
    """The flow. Subclasses supply a provider name and a scope list."""

    provider: ClassVar[str]
    """The `Provider` enum value this implements. Set by each subclass."""

    scope_list: ClassVar[list[str]]
    """What authorization is requested for. Set by each subclass."""

    def __init__(
        self,
        settings: Settings,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = settings.google_client_id
        self._client_secret = settings.google_client_secret.get_secret_value()
        self._timeout = timeout
        # For tests, as on `BaseClient`. The classification below — which errors
        # are permanent and which are worth retrying — is the most consequential
        # logic in this module and the least reachable without a seam, since
        # exercising it for real means persuading Google to reject a credential.
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)

    @property
    def provider_name(self) -> str:
        return self.provider

    @property
    def scopes(self) -> list[str]:
        return list(self.scope_list)

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        """Where to send the browser.

        See the module docstring for the two parameters that are easy to omit and
        expensive to have omitted.
        """
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(self.scope_list),
                "state": state,
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> TokenGrant:
        """Trade the one-time code for tokens, then find out whose they are."""
        payload = await self._token_request(
            {
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            }
        )
        grant = TokenGrant.from_response(payload)

        return TokenGrant(
            access_token=grant.access_token,
            refresh_token=grant.refresh_token,
            expires_at=grant.expires_at,
            scopes=grant.scopes,
            external_account_id=await self._account_email(grant.access_token),
        )

    async def refresh(self, refresh_token: str) -> TokenGrant:
        """Mint a new access token.

        The response carries no `refresh_token`, by design on Google's side. This
        method reports that faithfully — `TokenGrant.refresh_token` is None — and
        it is the *caller's* job to keep the existing one. Substituting the input
        here would look helpful and would hide the bug from every test, because a
        caller that overwrites blindly would then appear to work.
        """
        payload = await self._token_request(
            {
                "refresh_token": refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
            }
        )
        return TokenGrant.from_response(payload)

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        """POST to the token endpoint and classify what comes back."""
        async with self._client() as client:
            try:
                response = await client.post(TOKEN_URL, data=data)
            except httpx.HTTPError as error:
                message = f"Could not reach Google's token endpoint: {error}"
                raise OAuthError(message) from error

        payload = json_or_empty(response)
        error_code = payload.get("error")

        if error_code in REVOKED_ERRORS:
            # HTTP 400, not 401 — which is why the body decides and the status
            # code does not. See the module docstring.
            message = f"Google rejected the credential permanently ({error_code})."
            raise OAuthRevokedError(message)

        if response.is_error:
            message = f"Google's token endpoint returned {response.status_code}."
            raise OAuthError(message)

        if "access_token" not in payload:
            # A 200 with no token. Raised here rather than allowed to surface as a
            # `KeyError` three frames later, where the traceback would name a
            # dataclass instead of the provider that misbehaved.
            message = "Google's token response contained no access_token."
            raise OAuthError(message)

        return payload

    async def _account_email(self, access_token: str) -> str | None:
        """Which Google account this credential belongs to.

        Failure here is deliberately *not* fatal. The address is for humans to
        read, and a connection that works but cannot label itself is far better
        than throwing away an authorization the user just completed because a
        secondary lookup timed out.
        """
        async with self._client() as client:
            try:
                response = await client.get(
                    USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
                )
            except httpx.HTTPError:
                return None

        if response.is_error:
            return None

        email = json_or_empty(response).get("email")
        return str(email) if email else None


def json_or_empty(response: httpx.Response) -> dict[str, Any]:
    """Parse a JSON body, or return `{}` if it is not JSON at all.

    Google's load balancers return HTML during an incident, and a
    `JSONDecodeError` escaping from here would surface as a 500 mentioning
    neither Google nor OAuth.
    """
    try:
        payload: Any = response.json()
    except ValueError:
        return {}

    return payload if isinstance(payload, dict) else {}
