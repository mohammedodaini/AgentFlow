"""Stripe Connect OAuth — where the access token is a live secret API key.

Layer: integrations. Implements `OAuthProvider`; stores nothing.

**This is the most dangerous credential in the system, by some distance.**
------------------------------------------------------------------------
Google hands back a token scoped to a calendar. Slack hands back a token scoped
to reading channel names. Stripe hands back `sk_live_…` — an ordinary Stripe
secret key for the connected account, the same string that account's own
engineers keep in their production environment.

What bounds it is the `read_only` scope requested below, and *nothing else*. A
`read_write` grant would let this application create charges and issue refunds
against somebody else's business. That is why the scope is a module constant with
this paragraph attached rather than a parameter, and why `StripeClient` has no
method that is not a GET.

Stripe is also the one provider here where read-only is a **first-class grant**.
GitHub has no read-only scope for private repositories (see `github/oauth.py`),
so M14 asks it for almost nothing; Stripe offers exactly the right grant, and
asking for more would be a choice rather than a compromise.

The credential does not expire, and the refresh token is not like Google's
--------------------------------------------------------------------------
A Connect access token stays valid until the account disconnects. The
`refresh_token` Stripe returns is a long-lived credential for minting new access
tokens, and unlike Google's it is *not* re-issued on every use — the same
preservation rule in `IntegrationService._store_grant` covers both.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import Settings
from app.integrations.base import (
    DEFAULT_TIMEOUT_SECONDS,
    OAuthError,
    OAuthRevokedError,
    TokenGrant,
)

AUTHORIZE_URL = "https://connect.stripe.com/oauth/authorize"
TOKEN_URL = "https://connect.stripe.com/oauth/token"  # noqa: S105 — a URL, not a secret

SCOPES = ["read_only"]
"""Read-only, and see the module docstring for why this line matters more than
most. `read_write` is the alternative Stripe offers, and it permits creating
charges and refunds against the connected business."""

REVOKED_ERRORS = {"invalid_grant", "unsupported_grant_type", "invalid_client"}
"""Stripe's codes for a credential or code that will not work again.

`invalid_client` is here because Stripe returns it when the *platform's* secret
key is wrong — which is not recoverable by retrying either, and is a
misconfiguration a retry loop would hammer the endpoint about.
"""


class StripeOAuth:
    """The real provider. Needs `STRIPE_CLIENT_ID` and `STRIPE_CLIENT_SECRET`."""

    def __init__(
        self,
        settings: Settings,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = settings.stripe_client_id
        self._client_secret = settings.stripe_client_secret.get_secret_value()
        self._timeout = timeout
        self._transport = transport

    @property
    def provider_name(self) -> str:
        return "stripe"

    @property
    def scopes(self) -> list[str]:
        return list(SCOPES)

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        """Where to send the browser.

        `client_id` here is Stripe's `ca_…` Connect application id, **not** a
        publishable or secret key. They are three different strings from three
        different pages of the dashboard, and putting a `pk_…` here produces an
        authorize page that simply says the application does not exist.
        """
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "state": state,
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> TokenGrant:
        """Trade the code for a connected-account key.

        No follow-up identity call: `stripe_user_id` — the `acct_…` id — comes
        back in the token response, and it is what the Stripe dashboard shows, so
        it is the string a user can actually match against their own account.
        """
        del redirect_uri  # Stripe does not require it on the token exchange.

        payload = await self._token_request({"grant_type": "authorization_code", "code": code})
        grant = TokenGrant.from_response(payload)

        return TokenGrant(
            access_token=grant.access_token,
            refresh_token=grant.refresh_token,
            expires_at=grant.expires_at,
            scopes=grant.scopes or list(SCOPES),
            external_account_id=_account_id(payload),
        )

    async def refresh(self, refresh_token: str) -> TokenGrant:
        """Mint a new connected-account key from the long-lived refresh token."""
        payload = await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        return TokenGrant.from_response(payload, external_account_id=_account_id(payload))

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        """POST to Stripe and classify what comes back.

        The platform's secret key authenticates this call as a bearer token.
        Stripe accepts it in the body as `client_secret` too; the header is used
        because a body parameter ends up in more logs than a header does.
        """
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await client.post(
                    TOKEN_URL,
                    data=data,
                    headers={"Authorization": f"Bearer {self._client_secret}"},
                )
            except httpx.HTTPError as error:
                message = f"Could not reach Stripe's token endpoint: {error}"
                raise OAuthError(message) from error

        payload = _json_or_empty(response)
        error_code = payload.get("error")

        if error_code in REVOKED_ERRORS:
            message = f"Stripe rejected the credential permanently ({error_code})."
            raise OAuthRevokedError(message)

        if response.is_error:
            message = f"Stripe's token endpoint returned {response.status_code}."
            raise OAuthError(message)

        if "access_token" not in payload:
            message = "Stripe's token response contained no access_token."
            raise OAuthError(message)

        return payload


def _account_id(payload: dict[str, Any]) -> str | None:
    """The `acct_…` id of the connected account."""
    account = payload.get("stripe_user_id")
    return str(account) if account else None


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    """Parse a JSON body, or `{}` if it is not JSON at all."""
    try:
        payload: Any = response.json()
    except ValueError:
        return {}

    return payload if isinstance(payload, dict) else {}
