"""Connecting an external account, and keeping the credential alive.

Layer: services. Owns the rows, the encryption calls and the transaction;
delegates every provider-specific detail to `app/integrations/<provider>/oauth.py`.

The `state` parameter is not decoration — it is the whole security model
---------------------------------------------------------------------
An OAuth callback arrives as a **browser redirect from Google**. It carries no
`Authorization` header, no `X-Organization-Id`, and nothing else we issued. From
this application's point of view it is an unauthenticated request from a stranger
saying "here is an authorization code, please store it".

`state` is the only thread connecting that request back to the person who started
the flow, so it has to do four jobs at once:

1. **Be unguessable** — `secrets.token_urlsafe(32)`, so it cannot be forged.
2. **Carry the binding** — which organization, which user, which provider. The
   callback cannot be trusted to tell us, because anyone can call it.
3. **Expire** — an abandoned attempt must not be resumable from a URL somebody
   later finds in a browser history or a proxy log.
4. **Work exactly once** — deleted the moment it is consumed.

Without (1) and (2), an attacker sends a victim a crafted callback URL and *their*
Google account becomes the one connected to the victim's organization — after
which the agent reads the attacker's calendar and, from M12, writes to it. That
attack is login CSRF, it needs no access to our systems, and the `state` check is
the entire defence.

Redis rather than a table, for the reason `app/db/redis.py` gives: this is data
that may disappear. A lost state means one connect attempt has to be restarted,
which is the cheapest possible failure — and self-expiring entries mean nothing
accumulates.
"""

from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AuthenticationError, NotFoundError
from app.core.security import decrypt_secret, encrypt_secret
from app.integrations import OAuthRegistry
from app.integrations.base import OAuthError, OAuthProvider, OAuthRevokedError, TokenGrant
from app.models.integration import Integration, IntegrationStatus, Provider
from app.models.oauth_token import OAuthToken
from app.repositories.integration_repository import IntegrationRepository

logger = structlog.get_logger(__name__)

STATE_PREFIX = "oauth:state:"
"""Namespaced, because this Redis database also holds the M3 refresh-token
denylist and arq's queues. A bare random key would still work, and would make
`KEYS *` during an incident unreadable."""

CALLBACK_PATH = "/api/v1/integrations/{provider}/callback"
"""Where Google is told to send the browser back.

Built from `oauth_redirect_base_url` rather than from the incoming request,
because Google matches the redirect URI against its registered value *exactly* —
and deriving it from a `Host` or `X-Forwarded-Host` header would let a spoofed
header change where the authorization code is delivered.
"""


@dataclass(frozen=True)
class PendingConnect:
    """What `begin_connect` hands back: where to send the user, and the state."""

    authorize_url: str
    state: str


class IntegrationService:
    """The OAuth flow, the token store, and the refresh path."""

    def __init__(
        self,
        session: AsyncSession,
        redis: Redis,
        registry: OAuthRegistry,
        settings: Settings,
    ) -> None:
        self._session = session
        self._integrations = IntegrationRepository(session)
        self._redis = redis
        self._registry = registry
        self._settings = settings

    # -- connect ----------------------------------------------------------

    async def begin_connect(
        self, organization_id: uuid.UUID, user_id: uuid.UUID | None, provider: Provider
    ) -> PendingConnect:
        """Start a connect flow and return the URL to send the browser to."""
        oauth = self._provider(provider)
        state = secrets.token_urlsafe(32)

        # Written to Redis *before* the URL is handed out. The other order has a
        # race that only appears under load: a fast user — or a browser
        # pre-fetching the redirect — can complete the round trip to Google and
        # reach the callback before the write lands, and an entirely legitimate
        # flow fails with "unknown state".
        await self._redis.setex(
            f"{STATE_PREFIX}{state}",
            self._settings.oauth_state_ttl_seconds,
            json.dumps(
                {
                    "organization_id": str(organization_id),
                    "user_id": str(user_id) if user_id else None,
                    "provider": provider.value,
                }
            ),
        )

        logger.info(
            "integration.connect_started",
            organization_id=str(organization_id),
            provider=provider.value,
        )
        return PendingConnect(
            authorize_url=oauth.authorize_url(
                state=state, redirect_uri=self._redirect_uri(provider)
            ),
            state=state,
        )

    async def complete_callback(self, provider: Provider, *, state: str, code: str) -> Integration:
        """Verify the state, exchange the code, and store the credential.

        The order is deliberate: **state first, always.** Exchanging the code
        before checking would mean an attacker's forged callback caused us to talk
        to Google and mint a real credential, which we would then be holding with
        nowhere legitimate to put it.
        """
        binding = await self._consume_state(state)

        if binding["provider"] != provider.value:
            # The state was issued for a different provider. Nothing good explains
            # that, and answering with the same message as every other rejected
            # callback means an attacker learns nothing about which states exist.
            message = "This authorization request is not valid."
            raise AuthenticationError(message)

        organization_id = uuid.UUID(binding["organization_id"])
        user_id = uuid.UUID(binding["user_id"]) if binding["user_id"] else None

        oauth = self._provider(provider)
        grant = await oauth.exchange_code(code=code, redirect_uri=self._redirect_uri(provider))

        integration = await self._integrations.upsert_active(
            organization_id=organization_id,
            provider=provider,
            connected_by=user_id,
            scopes=grant.scopes or oauth.scopes,
            external_account_id=grant.external_account_id,
        )
        self._store_grant(integration, grant)
        await self._session.flush()

        logger.info(
            "integration.connected",
            organization_id=str(organization_id),
            provider=provider.value,
            account=grant.external_account_id,
            scopes=len(grant.scopes),
        )
        return integration

    # -- use --------------------------------------------------------------

    async def get_fresh_token(
        self, organization_id: uuid.UUID, provider: Provider
    ) -> tuple[Integration, str]:
        """An access token that is valid *now*, refreshing if it has to.

        Checked before use rather than after a 401, because a 401 from a provider
        is ambiguous: expired and revoked look identical over HTTP, and one is
        routine while the other must stop the integration.
        """
        integration = await self._active(organization_id, provider)
        token = integration.tokens

        if token is None:
            message = "This integration has no stored credential. Reconnect it."
            raise NotFoundError(message)

        if not token.needs_refresh():
            return integration, decrypt_secret(token.access_token)

        if token.refresh_token is None:
            # Nothing to refresh with, and the access token has expired. Terminal
            # rather than transient, so it is recorded as such instead of being
            # retried by every caller for the rest of the row's life.
            await self._mark_revoked(integration, reason="no_refresh_token")
            message = "This integration expired and cannot be refreshed. Reconnect it."
            raise NotFoundError(message)

        try:
            grant = await self._provider(provider).refresh(decrypt_secret(token.refresh_token))
        except OAuthRevokedError:
            # The user revoked access, changed their password, or left it unused
            # for six months. Normal events on their side, and no retry helps.
            await self._mark_revoked(integration, reason="invalid_grant")
            message = "Access to this account was revoked. Reconnect it."
            raise NotFoundError(message) from None

        self._store_grant(integration, grant)
        await self._session.flush()

        logger.info(
            "integration.refreshed",
            organization_id=str(organization_id),
            provider=provider.value,
        )
        return integration, grant.access_token

    # -- manage -----------------------------------------------------------

    async def list_for_organization(self, organization_id: uuid.UUID) -> list[Integration]:
        return await self._integrations.list_for_organization(organization_id)

    async def disconnect(
        self, organization_id: uuid.UUID, integration_id: uuid.UUID
    ) -> Integration:
        """Turn a connection off and destroy the credential.

        The row survives with `DISCONNECTED` — "who connected our calendar, and
        when was it removed?" is an audit question a deleted row cannot answer —
        but the *token* is deleted outright. Keeping a credential nobody intends to
        use is keeping a liability: it can still be leaked, and it can still be
        exchanged for access to somebody's account.
        """
        integration = await self._integrations.get(organization_id, integration_id)

        if integration is None:
            message = "Integration not found."
            raise NotFoundError(message)

        integration.status = IntegrationStatus.DISCONNECTED
        integration.tokens = None
        await self._session.flush()

        logger.info(
            "integration.disconnected",
            organization_id=str(organization_id),
            integration_id=str(integration_id),
        )
        return integration

    # -- internals --------------------------------------------------------

    async def _consume_state(self, state: str) -> dict[str, Any]:
        """Read the binding for `state` and delete it, atomically.

        `GETDEL` rather than a read followed by a delete: two callbacks arriving
        together — a double-clicked consent screen, a browser retrying — would
        both pass a read-then-delete check, and the second would try to connect
        using a code the first already spent. One round trip, one winner.
        """
        raw = await self._redis.getdel(f"{STATE_PREFIX}{state}")

        if raw is None:
            # Covers every failure identically: forged, expired, already used, or
            # never issued. One message on purpose — distinguishing them would let
            # an attacker probe which states exist.
            message = "This authorization request is not valid."
            raise AuthenticationError(message)

        binding: dict[str, Any] = json.loads(raw)
        return binding

    def _store_grant(self, integration: Integration, grant: TokenGrant) -> None:
        """Write a credential, encrypted, keeping the refresh token if reissued.

        **The refresh token is preserved when the provider does not send a new
        one**, and that single conditional is the most consequential line in this
        milestone. Google returns no `refresh_token` on a refresh; code that
        assigned the grant wholesale would write NULL over a long-lived
        credential. Everything keeps working for up to an hour — then the access
        token expires, there is nothing to refresh with, and the integration dies
        nowhere near the change that caused it.
        """
        token = integration.tokens

        if token is None:
            token = OAuthToken(integration_id=integration.id, access_token="")
            integration.tokens = token

        token.access_token = encrypt_secret(grant.access_token)
        token.expires_at = grant.expires_at

        if grant.refresh_token is not None:
            token.refresh_token = encrypt_secret(grant.refresh_token)

    async def _mark_revoked(self, integration: Integration, *, reason: str) -> None:
        """Record that a credential is dead, and drop it.

        The token is removed rather than kept beside the `REVOKED` status: nobody
        legitimate can use it again, so retaining it stores risk and nothing else.
        """
        integration.status = IntegrationStatus.REVOKED
        integration.tokens = None
        await self._session.flush()

        logger.warning(
            "integration.revoked",
            integration_id=str(integration.id),
            provider=integration.provider.value,
            reason=reason,
        )

    async def _active(self, organization_id: uuid.UUID, provider: Provider) -> Integration:
        integration = await self._integrations.get_active(organization_id, provider)

        if integration is None:
            message = f"No active {provider.value} integration for this organization."
            raise NotFoundError(message)

        return integration

    def _provider(self, provider: Provider) -> OAuthProvider:
        oauth = self._registry.get(provider)

        if oauth is None:
            message = (
                f"{provider.value} is not configured on this deployment. "
                "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET."
            )
            raise OAuthError(message)

        return oauth

    def _redirect_uri(self, provider: Provider) -> str:
        base = self._settings.oauth_redirect_base_url.rstrip("/")
        return f"{base}{CALLBACK_PATH.format(provider=provider.value)}"
