"""Slack OAuth v2, and the one thing about Slack that breaks every HTTP client.

Layer: integrations. Implements `OAuthProvider`; stores nothing.

**Slack reports failure with HTTP 200.**
--------------------------------------
Every error from `oauth.v2.access` — a reused code, a bad client secret, a
revoked token — arrives as `200 OK` carrying `{"ok": false, "error": "..."}`.
`response.is_error` is False for all of them.

That single fact invalidates the shape of `GoogleCalendarOAuth`, which checks the
status code first and reads the body second. Written the same way here, a failed
exchange would sail past every check, reach `TokenGrant.from_response`, and raise
`KeyError: 'access_token'` three frames away from the provider that refused us —
which is exactly the failure `_token_request` already refuses to allow for Google.

So `ok` is checked *before* the status code, and the status code is a fallback for
the case Slack is not answering at all.

Bot tokens do not expire, and there is no refresh token
-------------------------------------------------------
Unless a workspace has token rotation switched on, `oauth.v2.access` returns an
`xoxb-` bot token with **no `expires_in` and no `refresh_token`**. It stays valid
until somebody uninstalls the app.

M11 built the token store around Google, where every credential expires and every
credential can be refreshed. `OAuthToken.needs_refresh` treats a NULL `expires_at`
as expired — the safe direction for Google, and catastrophic here: the first use
of a freshly connected Slack workspace would find no refresh token, mark the
integration REVOKED, and tell the user to reconnect. Forever. See
`app/models/oauth_token.py` and ADR-0017.

`refresh()` therefore raises rather than pretending. A Slack integration that
reaches it is one whose access token genuinely expired, and the only path back is
a new authorization.
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

AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
TOKEN_URL = "https://slack.com/api/oauth.v2.access"  # noqa: S105 — a URL, not a secret

SCOPES = ["channels:read", "team:read"]
"""Read-only, and deliberately so.

`chat:write` is the scope everyone reaches for, and M14 does not request it — the
same argument M11 made about `calendar.readonly`: a write scope granted a
milestone before anything can use it means every connected workspace has already
handed an agent permission to post as the app, with nothing but the absence of a
call site standing in the way.

`channels:read` lists public channels. `team:read` names the workspace, which is
how somebody with two Slack workspaces can tell which one they connected — the
job `userinfo.email` does for Google.
"""

REVOKED_ERRORS = {
    "invalid_auth",
    "token_revoked",
    "token_expired",
    "account_inactive",
    "invalid_grant",
    "invalid_code",
    "code_already_used",
}
"""Slack's vocabulary for "this will never work"; all delivered with HTTP 200.

`invalid_code` and `code_already_used` are here because an authorization code is
single-use: a double-submitted callback must be a permanent refusal, not
something a retry loop keeps attempting.
"""


class SlackOAuth:
    """The real provider. Needs `SLACK_CLIENT_ID` and `SLACK_CLIENT_SECRET`."""

    def __init__(
        self,
        settings: Settings,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = settings.slack_client_id
        self._client_secret = settings.slack_client_secret.get_secret_value()
        self._timeout = timeout
        self._transport = transport

    @property
    def provider_name(self) -> str:
        return "slack"

    @property
    def scopes(self) -> list[str]:
        return list(SCOPES)

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        """Where to send the browser.

        `scope` is the *bot* scope list. Slack has a second parameter,
        `user_scope`, which requests permissions to act as the human rather than
        as the app; it is omitted, and that omission is the reason the token this
        flow yields cannot read anybody's DMs.
        """
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "scope": ",".join(SCOPES),
                "state": state,
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> TokenGrant:
        """Trade the code for a bot token.

        No follow-up call to identify the workspace: unlike Google, Slack returns
        `team` in the token response itself. One fewer request, and one fewer way
        for a connection to succeed while failing to say whose it is.
        """
        payload = await self._token_request(
            {
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": redirect_uri,
            }
        )
        grant = TokenGrant.from_response(payload)

        return TokenGrant(
            access_token=grant.access_token,
            refresh_token=grant.refresh_token,
            expires_at=grant.expires_at,
            scopes=grant.scopes,
            external_account_id=_workspace_name(payload),
        )

    async def refresh(self, refresh_token: str) -> TokenGrant:
        """Only reachable on a rotation-enabled workspace.

        Raising `OAuthRevokedError` for the ordinary case is the honest answer
        rather than a defensive one: a non-rotating Slack install has no refresh
        token at all, so anything that gets here holds a credential that expired
        with no way to renew it. Reconnecting is the only fix, and that is what
        `OAuthRevokedError` means everywhere else in this package.
        """
        payload = await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
        )
        return TokenGrant.from_response(payload, external_account_id=_workspace_name(payload))

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        """POST to Slack and classify what comes back — body first, status second."""
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await client.post(TOKEN_URL, data=data)
            except httpx.HTTPError as error:
                message = f"Could not reach Slack's token endpoint: {error}"
                raise OAuthError(message) from error

        payload = _json_or_empty(response)

        # Before the status code, not after. See the module docstring: every
        # Slack failure is an HTTP 200.
        if payload.get("ok") is False:
            error_code = str(payload.get("error", "unknown"))

            if error_code in REVOKED_ERRORS:
                message = f"Slack rejected the credential permanently ({error_code})."
                raise OAuthRevokedError(message)

            message = f"Slack refused the request ({error_code})."
            raise OAuthError(message)

        if response.is_error:
            # Reached when Slack is genuinely down and something other than the
            # API answered — a load balancer, a proxy, an error page.
            message = f"Slack's token endpoint returned {response.status_code}."
            raise OAuthError(message)

        if "access_token" not in payload:
            message = "Slack's token response contained no access_token."
            raise OAuthError(message)

        return payload


def _workspace_name(payload: dict[str, Any]) -> str | None:
    """Which workspace this is, for a human to read.

    Falls back to the team id when the name is absent, because "connected to
    T024BE7LD" is unhelpful but still identifies one workspace, whereas None
    identifies nothing.
    """
    team = payload.get("team")

    if not isinstance(team, dict):
        return None

    name = team.get("name") or team.get("id")
    return str(name) if name else None


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    """Parse a JSON body, or `{}` if it is not JSON at all."""
    try:
        payload: Any = response.json()
    except ValueError:
        return {}

    return payload if isinstance(payload, dict) else {}
