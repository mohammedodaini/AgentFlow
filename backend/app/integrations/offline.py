"""An authorization server that lives in memory. Development and tests only.

Layer: integrations. The counterpart to `HashingEmbedder` (ADR-0009) and
`OfflineLLM` (ADR-0010), and the most useful of the three — because OAuth is the
one feature here that is *impossible* to exercise honestly without something like
it: the real flow needs a browser, a Google account, a registered redirect URI and
a human clicking consent.

What it actually implements
---------------------------
A real authorization-code flow, minus the human. It issues single-use codes,
exchanges them for tokens, expires those tokens, refreshes them, and can have its
refresh token revoked out from under the application — which is every state the
service has to handle.

Two behaviours are modelled deliberately, because they are where real integrations
break:

**Codes are single-use.** Replaying one fails. A callback URL sits in browser
history, in a proxy log and in a `Referer` header, and an authorization server
that honoured it twice would let anyone who found it mint a credential.

**Refreshing returns no new refresh token.** That is what Google does, and it is
the single most common way a token store corrupts itself: code that writes the
whole grant back on every refresh overwrites the long-lived refresh token with
NULL, and the integration works perfectly until the access token expires an hour
later. Making that the *default* behaviour of the double means any code path that
gets it wrong fails immediately rather than in an hour.

**Some credentials never expire at all** (`perpetual=True`, added at M14). Slack,
Notion and GitHub issue a token with no `expires_in` and no refresh token, and
under M11's rules the first use of one marked the integration REVOKED — see
`OAuthToken.needs_refresh`. That bug was invisible offline because this double
always behaved like Google. A test double that can only produce the shape the code
already handles is a test double that certifies the bug.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from app.integrations.base import OAuthRevokedError, TokenGrant

AUTHORIZE_URL = "https://offline.agentflow.test/authorize"
"""A `.test` domain, which RFC 2606 reserves and no DNS will ever resolve.

Deliberate: if this URL ever escapes into a real deployment, the browser fails
loudly instead of quietly reaching somebody's server with an authorization
request.
"""

CODE_PREFIX = "offline-code-"
ACCESS_PREFIX = "offline-access-"  # noqa: S105 — a token *prefix*, not a credential
REFRESH_PREFIX = "offline-refresh-"  # noqa: S105 — likewise

DEFAULT_ACCOUNT = "ada@example.test"
DEFAULT_EXPIRES_IN = 3600
"""One hour, matching Google. The number matters because `needs_refresh` reasons
about it, and a double that never expired would leave the refresh path
untested."""


class OfflineOAuthProvider:
    """A provider whose authorization server is this object.

    Stateful, and scoped to one instance: issued codes, live refresh tokens and
    revocations all live here. The application builds one per process, so a test
    can reach the same instance and revoke a credential the way a user would from
    their Google account page.
    """

    def __init__(
        self,
        provider_name: str,
        scopes: list[str],
        *,
        account: str = DEFAULT_ACCOUNT,
        expires_in: int = DEFAULT_EXPIRES_IN,
        perpetual: bool = False,
    ) -> None:
        self._provider_name = provider_name
        self._scopes = scopes
        self._account = account
        self._expires_in = expires_in
        # Slack, Notion and GitHub, whose tokens have no expiry and no refresh
        # token. The registry sets this per provider so that running offline
        # reproduces the credential *shape* each real provider issues, not just
        # the flow.
        self._perpetual = perpetual
        self._unused_codes: set[str] = set()
        self._live_refresh_tokens: set[str] = set()

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def scopes(self) -> list[str]:
        return list(self._scopes)

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        """The URL a browser would be sent to, with the code already issued.

        A real authorization server issues the code *after* the user consents.
        Issuing it up front is the one place this double diverges from reality —
        and it is what lets a test follow the redirect itself instead of driving a
        browser. The code is still single-use and still has to be exchanged, so
        everything downstream of consent is genuinely exercised.
        """
        code = f"{CODE_PREFIX}{secrets.token_urlsafe(16)}"
        self._unused_codes.add(code)

        query = urlencode(
            {
                "state": state,
                "redirect_uri": redirect_uri,
                "scope": " ".join(self._scopes),
                "code": code,
                "response_type": "code",
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> TokenGrant:
        """Trade a code for a grant. Each code works exactly once."""
        del redirect_uri

        if code not in self._unused_codes:
            message = "Authorization code is unknown or already used."
            raise OAuthRevokedError(message)

        self._unused_codes.discard(code)

        if self._perpetual:
            # No expiry and no refresh token — the shape a Slack bot token, a
            # Notion workspace token and a GitHub OAuth App token all have.
            return TokenGrant(
                access_token=f"{ACCESS_PREFIX}{secrets.token_urlsafe(16)}",
                refresh_token=None,
                expires_at=None,
                scopes=list(self._scopes),
                external_account_id=self._account,
            )

        refresh_token = f"{REFRESH_PREFIX}{secrets.token_urlsafe(16)}"
        self._live_refresh_tokens.add(refresh_token)

        return TokenGrant(
            access_token=f"{ACCESS_PREFIX}{secrets.token_urlsafe(16)}",
            refresh_token=refresh_token,
            expires_at=datetime.now(UTC) + timedelta(seconds=self._expires_in),
            scopes=list(self._scopes),
            external_account_id=self._account,
        )

    async def refresh(self, refresh_token: str) -> TokenGrant:
        """A new access token — and, like Google, no new refresh token.

        A perpetual provider refuses outright, as Notion's does: there is no
        refresh grant to attempt, so anything reaching here is a caller that
        believed a permanent credential had expired.
        """
        if self._perpetual:
            message = f"{self._provider_name} credentials cannot be refreshed."
            raise OAuthRevokedError(message)

        if refresh_token not in self._live_refresh_tokens:
            message = "invalid_grant: the refresh token has been revoked."
            raise OAuthRevokedError(message)

        return TokenGrant(
            access_token=f"{ACCESS_PREFIX}{secrets.token_urlsafe(16)}",
            refresh_token=None,
            expires_at=datetime.now(UTC) + timedelta(seconds=self._expires_in),
            scopes=list(self._scopes),
            external_account_id=self._account,
        )

    def revoke(self, refresh_token: str) -> None:
        """Kill a credential, the way a user does from their account page.

        A test hook, and the reason this class is stateful at all. Revocation is
        the state a mocked HTTP call models badly: what matters is not that one
        request returned `invalid_grant`, but that the credential is dead from now
        on and every later attempt must reach the same conclusion.
        """
        self._live_refresh_tokens.discard(refresh_token)
