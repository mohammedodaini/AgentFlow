"""/integrations — the OAuth connect flow and connection management (M11).

Layer: api. Routes call `IntegrationService`, never a provider and never a
repository.

The callback is the only unauthenticated endpoint in this file, and that is not
an oversight
-----------------------------------------------------------------------------
Every other route here takes `CurrentMembership`. `/{provider}/callback` cannot:
it is reached by the **user's browser being redirected from Google**, so it
carries no `Authorization` header and no `X-Organization-Id`. Nothing we issued
survives the round trip except the `state` parameter.

So `state` *is* the authentication for this request. It is unguessable,
single-use, short-lived, and it carries the organization and user binding — see
`IntegrationService` for what an attacker gets if any of those four properties is
missing. Adding an auth dependency here would not make it safer; it would make the
endpoint unreachable and the feature would simply not work.

Why connect returns a URL instead of a redirect
-----------------------------------------------
A 302 in response to an XHR is followed by the browser invisibly, and the SPA
receives an opaque CORS error rather than a consent screen. Returning the URL lets
the client choose a full-page navigation or a popup. See `ConnectStart`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentMembership
from app.core.config import Settings, get_settings
from app.core.exceptions import NotFoundError
from app.db.deps import get_db
from app.db.redis import get_redis
from app.integrations import SUPPORTED_PROVIDERS, OAuthRegistry, get_oauth_registry
from app.integrations.google_calendar.client import GoogleCalendarClient
from app.models.integration import Provider
from app.schemas.integration import CalendarEventRead, ConnectStart, IntegrationRead
from app.services.integration_service import IntegrationService

router = APIRouter(prefix="/integrations", tags=["integrations"])

SessionDep = Annotated[AsyncSession, Depends(get_db)]
RedisDep = Annotated[Redis, Depends(get_redis)]
RegistryDep = Annotated[OAuthRegistry, Depends(get_oauth_registry)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _service(
    session: AsyncSession, redis: Redis, registry: OAuthRegistry, settings: Settings
) -> IntegrationService:
    return IntegrationService(session, redis, registry, settings)


def _supported(provider: Provider) -> Provider:
    """Reject a provider this deployment cannot drive, before anything else.

    `Provider` declares seven values so the *second* integration is a deploy rather
    than an `ALTER TYPE` migration. Only one is implemented, so asking to connect
    Slack has to be a clear 404 rather than a redirect to an authorization server
    nobody configured.
    """
    if provider not in SUPPORTED_PROVIDERS:
        message = f"{provider.value} cannot be connected yet."
        raise NotFoundError(message)

    return provider


@router.get("", summary="List connected accounts")
async def list_integrations(
    membership: CurrentMembership,
    session: SessionDep,
    redis: RedisDep,
    registry: RegistryDep,
    settings: SettingsDep,
) -> list[IntegrationRead]:
    """Every connection this organization has made, including broken ones.

    Revoked rows are included on purpose: "Google Calendar — needs reconnecting"
    is the most useful thing this endpoint can say, and filtering it out would
    render a broken integration as an absence indistinguishable from never having
    connected.
    """
    integrations = await _service(session, redis, registry, settings).list_for_organization(
        membership.organization_id
    )
    return [IntegrationRead.model_validate(integration) for integration in integrations]


@router.get("/{provider}/connect", summary="Begin connecting an account")
async def begin_connect(
    provider: Provider,
    membership: CurrentMembership,
    session: SessionDep,
    redis: RedisDep,
    registry: RegistryDep,
    settings: SettingsDep,
) -> ConnectStart:
    """Mint a `state`, remember what it is for, and return the consent URL."""
    pending = await _service(session, redis, registry, settings).begin_connect(
        membership.organization_id, membership.user_id, _supported(provider)
    )
    return ConnectStart(authorize_url=pending.authorize_url, provider=provider)


@router.get("/{provider}/callback", summary="Finish connecting an account")
async def complete_callback(
    provider: Provider,
    session: SessionDep,
    redis: RedisDep,
    registry: RegistryDep,
    settings: SettingsDep,
    state: Annotated[str, Query(description="The value issued by /connect")],
    code: Annotated[str, Query(description="Google's one-time authorization code")],
) -> IntegrationRead:
    """Exchange the code for tokens and store them, encrypted.

    **No auth dependency, deliberately.** See the module docstring: this request
    arrives from Google via the user's browser, so `state` is the only credential
    it can possibly carry — and it is checked first, before the code is exchanged
    for anything.
    """
    integration = await _service(session, redis, registry, settings).complete_callback(
        _supported(provider), state=state, code=code
    )
    return IntegrationRead.model_validate(integration)


@router.delete("/{integration_id}", summary="Disconnect an account")
async def disconnect(
    integration_id: uuid.UUID,
    membership: CurrentMembership,
    session: SessionDep,
    redis: RedisDep,
    registry: RegistryDep,
    settings: SettingsDep,
) -> IntegrationRead:
    """Mark the connection disconnected and destroy its credential.

    The row survives for the audit trail; the token does not. Keeping a credential
    nobody intends to use is keeping a liability.
    """
    integration = await _service(session, redis, registry, settings).disconnect(
        membership.organization_id, integration_id
    )
    return IntegrationRead.model_validate(integration)


@router.get("/google-calendar/events", summary="Read upcoming calendar events")
async def list_calendar_events(
    membership: CurrentMembership,
    session: SessionDep,
    redis: RedisDep,
    registry: RegistryDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[CalendarEventRead]:
    """Proof the credential works, end to end.

    Read-only, and the only provider call M11 ships. A write would need the write
    scope *and* an approval record, which is exactly M12 — building it now would
    mean the sole thing between an agent and somebody's diary was that no route
    called it yet.

    The token comes from `get_fresh_token`, so an expired credential is refreshed
    here rather than producing a 401 the caller has to interpret. A revoked one
    becomes a 404 telling the user to reconnect, which is the only action
    available to them.
    """
    _, access_token = await _service(session, redis, registry, settings).get_fresh_token(
        membership.organization_id, Provider.GOOGLE_CALENDAR
    )

    events = await GoogleCalendarClient().list_events(access_token, limit=limit)
    return [CalendarEventRead.model_validate(event) for event in events]
