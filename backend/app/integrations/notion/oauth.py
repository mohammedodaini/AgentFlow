"""Notion OAuth: HTTP Basic on the token endpoint, and no scopes at all.

Layer: integrations. Implements `OAuthProvider`; stores nothing.

Three ways Notion is not Google
-------------------------------
**The client credentials go in an `Authorization: Basic` header**, not in the
form body. Notion returns 401 for a request that puts them where Google, Slack
and GitHub all accept them — so this is the one provider where getting the
*location* of the secret wrong looks exactly like getting the secret itself
wrong.

**There are no scopes.** Notion's authorization model is a page picker: the user
chooses which pages and databases the integration may see, and the token response
carries no `scope` field. `SCOPES` is therefore empty, and that is the truthful
value — `Integration.scopes` will be `[]` for every Notion connection, which says
"this provider does not express permissions this way" rather than inventing a
label for something that does not exist.

**The token never expires and there is no refresh token.** Same shape as Slack
and GitHub, same collision with M11's assumption, same resolution — see
ADR-0017. `refresh()` here cannot even be attempted: Notion has no refresh grant
type, so it raises immediately rather than making a request that would 400.
"""

from __future__ import annotations

import base64
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

AUTHORIZE_URL = "https://api.notion.com/v1/oauth/authorize"
TOKEN_URL = "https://api.notion.com/v1/oauth/token"  # noqa: S105 — a URL, not a secret

SCOPES: list[str] = []
"""Empty, and deliberately not a placeholder.

Notion grants access per page rather than per permission. Writing something like
`["read_content"]` here would put a scope in `Integration.scopes` that Notion
never issued and that no check could ever be made against — a plausible-looking
lie in the one place a user goes to see what they granted.
"""

REVOKED_ERRORS = {"unauthorized", "invalid_grant", "restricted_resource"}
"""Notion's codes for a credential that will not start working again."""


class NotionOAuth:
    """The real provider. Needs `NOTION_CLIENT_ID` and `NOTION_CLIENT_SECRET`."""

    def __init__(
        self,
        settings: Settings,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = settings.notion_client_id
        self._client_secret = settings.notion_client_secret.get_secret_value()
        self._timeout = timeout
        self._transport = transport

    @property
    def provider_name(self) -> str:
        return "notion"

    @property
    def scopes(self) -> list[str]:
        return list(SCOPES)

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        """Where to send the browser.

        `owner=user` is required and has exactly one legal value; omitting it is a
        400 from the authorize endpoint, which the user sees rather than us.
        """
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "owner": "user",
                "state": state,
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> TokenGrant:
        """Trade the code for a workspace token."""
        payload = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            }
        )
        grant = TokenGrant.from_response(payload)

        return TokenGrant(
            access_token=grant.access_token,
            refresh_token=None,
            expires_at=None,
            scopes=[],
            external_account_id=_workspace_name(payload),
        )

    async def refresh(self, refresh_token: str) -> TokenGrant:
        """Unreachable by design: Notion issues no refresh tokens.

        Raising rather than making the request, because Notion's token endpoint
        would answer `400 invalid_request` for an unsupported `grant_type` — and
        that classifies as a transient `OAuthError`, inviting a retry of something
        that cannot ever succeed.
        """
        del refresh_token
        message = "Notion credentials cannot be refreshed. Reconnect the workspace."
        raise OAuthRevokedError(message)

    async def _token_request(self, body: dict[str, str]) -> dict[str, Any]:
        """POST with Basic auth, and classify what comes back."""
        credentials = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()

        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await client.post(
                    TOKEN_URL,
                    json=body,
                    headers={
                        "Authorization": f"Basic {credentials}",
                        "Content-Type": "application/json",
                    },
                )
            except httpx.HTTPError as error:
                message = f"Could not reach Notion's token endpoint: {error}"
                raise OAuthError(message) from error

        payload = _json_or_empty(response)
        error_code = payload.get("error") or payload.get("code")

        if error_code in REVOKED_ERRORS:
            message = f"Notion rejected the credential permanently ({error_code})."
            raise OAuthRevokedError(message)

        if response.is_error:
            message = f"Notion's token endpoint returned {response.status_code}."
            raise OAuthError(message)

        if "access_token" not in payload:
            message = "Notion's token response contained no access_token."
            raise OAuthError(message)

        return payload


def _workspace_name(payload: dict[str, Any]) -> str | None:
    """Which Notion workspace this is, for a human to read."""
    name = payload.get("workspace_name") or payload.get("workspace_id")
    return str(name) if name else None


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    """Parse a JSON body, or `{}` if it is not JSON at all."""
    try:
        payload: Any = response.json()
    except ValueError:
        return {}

    return payload if isinstance(payload, dict) else {}
