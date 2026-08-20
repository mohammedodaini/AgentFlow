"""Shared integration contracts: the OAuth provider seam and a base HTTP client.

Layer: integrations. **Rule: provider types never leak upward.** A Gmail message
dict, a Google Calendar event resource, an OAuth token response — each is
translated into our own shape at this boundary, so that swapping a provider, or
absorbing a breaking change in theirs, is work in one directory rather than
everywhere the data ended up.

The fourth protocol seam in this codebase, and the same argument every time
(ADR-0007 storage, ADR-0009 embeddings, ADR-0010 the LLM): there are no Google
credentials in this environment, and a test suite that needs a live consent
screen is a test suite nobody runs. `app/integrations/offline.py` is a working
authorization server that lives in memory, so the whole flow — state, code
exchange, expiry, refresh, revocation — is exercised on every run.

Two failure modes, deliberately different types
-----------------------------------------------
`OAuthError` means *something went wrong*: the network, a 500, a malformed
response. Retrying may help.

`OAuthRevokedError` means *the credential is dead*. Google returns
`invalid_grant` when a refresh token has been revoked from the user's account
page, expired through six months of disuse, or been invalidated by a password
change. **This is a normal state, not an incident.** Collapsing it into
`OAuthError` is the mistake that produces a background job retrying a credential
that can never work again, three times an hour, forever — while the user sees an
integration that claims to be connected and silently does nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

import httpx

from app.core.exceptions import AppError

DEFAULT_TIMEOUT_SECONDS = 15.0
"""A request with no timeout does not fail — it hangs, holding a connection and a
worker slot. The same reasoning as `llm_timeout_seconds` and
`arq_job_timeout_seconds`."""


class OAuthError(AppError):
    """A provider call failed in a way that might succeed if retried."""

    default_code = "oauth_error"


class OAuthRevokedError(OAuthError):
    """The credential is permanently dead; only reconnecting fixes it.

    A subclass, so a caller that only cares "did this fail?" still catches it,
    while the caller that must distinguish — `IntegrationService.get_fresh_token`,
    which marks the integration `REVOKED` — can.
    """

    default_code = "oauth_revoked"


@dataclass(frozen=True)
class TokenGrant:
    """What a provider hands back, in our shape rather than theirs.

    `expires_at` is absolute, converted from the provider's relative `expires_in`
    at the moment of receipt. Storing the relative value would make every later
    expiry check depend on knowing exactly when the response arrived — a fact
    nobody records, and one that a queue delay or a slow write quietly
    invalidates.
    """

    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scopes: list[str] = field(default_factory=list)
    external_account_id: str | None = None

    @classmethod
    def from_response(
        cls, payload: dict[str, Any], *, external_account_id: str | None = None
    ) -> TokenGrant:
        """Build a grant from a standard OAuth 2.0 token response.

        **`scope` is read from the response, never from what we asked for.** A
        user can untick a permission on the consent screen and Google returns the
        reduced set without complaint. A system that recorded its own request
        would believe it had access it does not have, and would discover the truth
        at the first API call rather than at the point somebody could be told.
        """
        expires_in = payload.get("expires_in")
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=int(expires_in))
            if expires_in is not None
            else None
        )
        return cls(
            access_token=str(payload["access_token"]),
            refresh_token=payload.get("refresh_token"),
            expires_at=expires_at,
            scopes=parse_scopes(payload.get("scope")),
            external_account_id=external_account_id,
        )


def parse_scopes(raw: Any) -> list[str]:
    """Split a provider's `scope` field, whichever separator it chose.

    RFC 6749 §3.3 says space-delimited. **Slack and GitHub both send commas**,
    and M11 shipped a bare `.split()` because Google obeys the spec — so the
    first Slack connection would have stored a single 35-character "scope"
    reading `chat:write,channels:read` and every question asked of that list
    would have had the wrong answer.

    Nothing would have raised. `Integration.scopes` is display-and-audit data, so
    the damage is a permissions list that is wrong in the one place a human goes
    to find out what they granted.

    Both separators are accepted here rather than configured per provider,
    because no scope any of these products issues contains a comma or a space —
    and a per-provider parser is one more thing for the sixth integration to
    forget.
    """
    if not raw:
        return []

    return [scope for scope in str(raw).replace(",", " ").split() if scope]


@runtime_checkable
class OAuthProvider(Protocol):
    """What `IntegrationService` needs from any OAuth provider.

    `runtime_checkable` so conformance is asserted in a test rather than
    discovered as an `AttributeError` during a callback the user is watching —
    the same reason `EmbeddingProvider` and `ObjectStorage` carry it.

    Notice what is *not* here: nothing that stores anything, and nothing that
    knows what an `Integration` is. A provider builds URLs and exchanges strings.
    Every decision about persistence, tenancy and status belongs to the service,
    which is what keeps adding the second provider a matter of writing one class.
    """

    @property
    def provider_name(self) -> str:
        """The `Provider` enum value this implements."""
        ...

    @property
    def scopes(self) -> list[str]:
        """What authorization will be requested for."""
        ...

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        """Where to send the user's browser to grant consent."""
        ...

    async def exchange_code(self, *, code: str, redirect_uri: str) -> TokenGrant:
        """Trade a one-time authorization code for tokens."""
        ...

    async def refresh(self, refresh_token: str) -> TokenGrant:
        """Mint a new access token. Raises `OAuthRevokedError` if it is dead."""
        ...


class BaseClient:
    """Shared httpx wiring for provider API clients.

    Holds no credential of its own: a bearer token is passed per call, because a
    client that captured one at construction would keep using it after a refresh
    replaced it — and the symptom would be intermittent 401s an hour after every
    reconnect.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        # `transport` is for tests. The alternative is patching
        # `httpx.AsyncClient` globally, which silences *every* HTTP call in the
        # process — including ones a test never meant to stub, so a real request
        # that should have failed quietly passes. A transport replaces only this
        # client's wire, and the response translation below still runs unchanged,
        # which is the part actually worth testing.
        self._transport = transport

    # -- what subclasses vary ---------------------------------------------

    extra_headers: dict[str, str] = {}
    """Headers every request to this provider must carry, beyond the bearer token.

    Added at M14, because two of the four new providers reject a request without
    one and neither failure names the cause. Notion answers 400 `validation_error`
    when `Notion-Version` is missing, and GitHub silently serves whichever API
    version it feels like unless `X-GitHub-Api-Version` pins it — so the second
    one is not an error at all, it is a response shape that changes under you on
    a date GitHub chooses.

    A class attribute rather than a constructor argument: it is a fact about the
    provider, not about the call, and passing it per request is how one call site
    ends up missing it.
    """

    forbidden_message = "The connected account does not permit this operation."
    """What a 403 means to the person who asked, in this provider's terms.

    **M11 put Google Calendar's wording here, in the shared base class**, so a
    403 from Slack would have told a user to reconnect their *calendar* to grant
    write access. It was correct when exactly one provider existed and became
    wrong the moment a second one did — which is the whole failure mode of
    hoisting a specific message into a general place.
    """

    def _headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}", **self.extra_headers}

    def _raise_for_status(self, response: httpx.Response, url: str) -> None:
        """Turn an HTTP failure into the right one of our two error types.

        Extracted at M14 so that a client which cannot use `get_json` — GitHub's
        collection endpoints return a bare JSON array, not an object — still
        classifies failures identically. The alternative was a second copy of
        this ladder, which is how one provider ends up treating a 403 as
        retryable while the other four do not.
        """
        if response.status_code == httpx.codes.UNAUTHORIZED:
            message = "The provider rejected our credential."
            raise OAuthRevokedError(message)

        if response.status_code == httpx.codes.FORBIDDEN:
            # Distinct from 401, and the distinction is the whole reason M12
            # widened the calendar scope. 401 means "this credential is not
            # valid"; 403 means "it is valid and does not permit this" — which is
            # what an account connected under a narrower scope gets. Telling a
            # user to reconnect is actionable; "the request failed" is not.
            #
            # The wording comes from `forbidden_message` rather than being spelled
            # out here. M11 hard-coded Google Calendar's, which meant a 403 from
            # Slack would have advised reconnecting a calendar.
            raise OAuthRevokedError(self.forbidden_message)

        if response.is_error:
            message = f"Request to {url} returned {response.status_code}."
            raise OAuthError(message)

    async def get_json(
        self, url: str, *, access_token: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """GET and parse JSON, translating transport failures into `OAuthError`.

        A 401 becomes `OAuthRevokedError`, because at this layer that is what it
        means: the service refreshes *before* calling, so a token rejected here
        was minted seconds ago and is not merely stale.
        """
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await client.get(url, params=params, headers=self._headers(access_token))
            except httpx.HTTPError as error:
                message = f"Request to {url} failed: {error}"
                raise OAuthError(message) from error

        self._raise_for_status(response, url)

        payload: dict[str, Any] = response.json()
        return payload

    async def post_json(
        self, url: str, *, access_token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """POST and parse JSON, classifying failures exactly as `get_json` does.

        A separate method rather than a `method=` parameter, because the two are
        not interchangeable at this layer: a GET is safe to retry and a POST is
        not. Anything that eventually adds retries must be able to tell them
        apart, and a shared method with a verb argument makes that distinction
        invisible at the call site.
        """
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await client.post(url, json=body, headers=self._headers(access_token))
            except httpx.HTTPError as error:
                message = f"Request to {url} failed: {error}"
                raise OAuthError(message) from error

        self._raise_for_status(response, url)

        payload: dict[str, Any] = response.json()
        return payload
