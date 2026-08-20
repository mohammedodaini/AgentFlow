"""GitHub OAuth, and the header without which the response is not JSON.

Layer: integrations. Implements `OAuthProvider`; stores nothing.

**`Accept: application/json` is not optional.**
----------------------------------------------
GitHub's token endpoint defaults to `application/x-www-form-urlencoded` — it
answers 200 with a body reading
`access_token=gho_x&scope=read%3Auser&token_type=bearer`. Parsing that as JSON
raises `ValueError`, which `_json_or_empty` swallows into `{}`, which becomes
"GitHub's token response contained no access_token". The error names the right
provider and the wrong cause, and no amount of checking the client secret fixes
it.

**Failures arrive as HTTP 200**, as with Slack: a reused code comes back
`{"error": "bad_verification_code"}` with a success status. So the body is
checked before the status code here too.

Scopes: why this asks for so little
-----------------------------------
GitHub's classic OAuth scopes have **no read-only option for private
repositories**. `repo` is read *and write* — to code, issues, pull requests,
webhooks and settings, across every repository the user can reach. There is no
narrower grant.

M14 is read-only, so it asks for `read:user` and nothing else, and
`GitHubClient.list_repositories` consequently sees public repositories only.
Requesting `repo` to render a list of names would mean every person who connected
GitHub had handed this application the ability to force-push to their employer's
codebase, in exchange for a page showing repository names. That trade is not
close, and it is the reason the listing is smaller than a user might expect —
which the milestone note says out loud rather than leaving as a puzzle.
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

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 — a URL
USER_URL = "https://api.github.com/user"

SCOPES = ["read:user"]
"""The identity of the account that connected, and nothing more.

See the module docstring for why there is no repository scope here. `read:user`
is what makes `external_account_id` a GitHub login rather than None, which is how
somebody with a personal and a work account can tell which one they connected.
"""

REVOKED_ERRORS = {
    "bad_verification_code",
    "bad_refresh_token",
    "incorrect_client_credentials",
    "unauthorized",
    "invalid_grant",
}


class GitHubOAuth:
    """The real provider. Needs `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET`."""

    def __init__(
        self,
        settings: Settings,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = settings.github_client_id
        self._client_secret = settings.github_client_secret.get_secret_value()
        self._timeout = timeout
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)

    @property
    def provider_name(self) -> str:
        return "github"

    @property
    def scopes(self) -> list[str]:
        return list(SCOPES)

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        """Where to send the browser."""
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(SCOPES),
                "state": state,
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> TokenGrant:
        """Trade the code for a token, then find out whose it is."""
        payload = await self._token_request(
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            }
        )
        grant = TokenGrant.from_response(payload)

        return TokenGrant(
            access_token=grant.access_token,
            refresh_token=grant.refresh_token,
            expires_at=grant.expires_at,
            scopes=grant.scopes,
            external_account_id=await self._account_login(grant.access_token),
        )

    async def refresh(self, refresh_token: str) -> TokenGrant:
        """Only meaningful when the app has expiring user tokens switched on.

        GitHub OAuth Apps issue non-expiring tokens by default, in which case
        nothing ever calls this. Apps that opt in get an eight-hour token *and* a
        six-month refresh token, and unlike Google GitHub does return a new
        refresh token here — which `IntegrationService._store_grant` writes,
        because it stores what it is given rather than assuming Google's rule.
        """
        payload = await self._token_request(
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        return TokenGrant.from_response(payload)

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        """POST to GitHub, asking for JSON, and classify what comes back."""
        async with self._client() as client:
            try:
                response = await client.post(
                    TOKEN_URL,
                    data=data,
                    # The header this whole module's docstring is about.
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPError as error:
                message = f"Could not reach GitHub's token endpoint: {error}"
                raise OAuthError(message) from error

        payload = _json_or_empty(response)
        error_code = payload.get("error")

        # Body before status: GitHub reports a bad code with HTTP 200.
        if error_code in REVOKED_ERRORS:
            message = f"GitHub rejected the credential permanently ({error_code})."
            raise OAuthRevokedError(message)

        if error_code:
            message = f"GitHub refused the request ({error_code})."
            raise OAuthError(message)

        if response.is_error:
            message = f"GitHub's token endpoint returned {response.status_code}."
            raise OAuthError(message)

        if "access_token" not in payload:
            message = "GitHub's token response contained no access_token."
            raise OAuthError(message)

        return payload

    async def _account_login(self, access_token: str) -> str | None:
        """Which GitHub account this is. Never fatal — see `GoogleCalendarOAuth`."""
        async with self._client() as client:
            try:
                response = await client.get(
                    USER_URL,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
            except httpx.HTTPError:
                return None

        if response.is_error:
            return None

        login = _json_or_empty(response).get("login")
        return str(login) if login else None


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    """Parse a JSON body, or `{}` if it is not JSON at all."""
    try:
        payload: Any = response.json()
    except ValueError:
        return {}

    return payload if isinstance(payload, dict) else {}
