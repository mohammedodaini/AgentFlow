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
            scopes=str(payload.get("scope", "")).split(),
            external_account_id=external_account_id,
        )


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
                response = await client.get(
                    url, params=params, headers={"Authorization": f"Bearer {access_token}"}
                )
            except httpx.HTTPError as error:
                message = f"Request to {url} failed: {error}"
                raise OAuthError(message) from error

        if response.status_code == httpx.codes.UNAUTHORIZED:
            message = "The provider rejected our credential."
            raise OAuthRevokedError(message)

        if response.is_error:
            message = f"Request to {url} returned {response.status_code}."
            raise OAuthError(message)

        payload: dict[str, Any] = response.json()
        return payload
