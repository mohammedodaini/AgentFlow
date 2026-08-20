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
from app.integrations.github.client import GitHubClient
from app.integrations.gmail.client import GmailClient
from app.integrations.google_calendar.client import GoogleCalendarClient
from app.integrations.notion.client import NotionClient
from app.integrations.slack.client import SlackClient
from app.integrations.stripe.client import StripeClient
from app.models.integration import Provider
from app.schemas.integration import (
    CalendarEventRead,
    ConnectStart,
    EmailMessageRead,
    GitHubRepositoryRead,
    IntegrationRead,
    NotionPageRead,
    ProviderRead,
    SlackChannelRead,
    StripeChargeRead,
)
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


@router.get("/providers", summary="List connectable providers")
async def list_providers(
    membership: CurrentMembership,
    registry: RegistryDep,
) -> list[ProviderRead]:
    """What this deployment can connect, and what each will ask permission for.

    Derived from the registry rather than from `SUPPORTED_PROVIDERS`, so it
    reflects what is *configured* — a provider whose client id is unset is absent
    here, which is what stops the UI offering a button that leads to a 404.

    Authenticated, though it exposes no tenant data: it describes the deployment,
    and an unauthenticated version would tell an anonymous caller exactly which
    third-party integrations this organization has credentials for.
    """
    del membership

    return [
        ProviderRead(provider=provider, scopes=list(oauth.scopes))
        for provider in registry.configured()
        if (oauth := registry.get(provider)) is not None
    ]


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
    service = _service(session, redis, registry, settings)

    async with service.using(membership.organization_id, Provider.GOOGLE_CALENDAR) as token:
        events = await GoogleCalendarClient().list_events(token, limit=limit)

    return [CalendarEventRead.model_validate(event) for event in events]


@router.get("/slack/channels", summary="List Slack channels")
async def list_slack_channels(
    membership: CurrentMembership,
    session: SessionDep,
    redis: RedisDep,
    registry: RegistryDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[SlackChannelRead]:
    """Public channels in the connected workspace. Read-only.

    Nothing here posts. `SCOPES` in `integrations/slack/oauth.py` does not request
    `chat:write`, for the reason M11 gave about the calendar: a write permission
    granted a milestone before anything uses it means every connected workspace
    has already said yes to something nobody has reviewed.
    """
    service = _service(session, redis, registry, settings)

    async with service.using(membership.organization_id, Provider.SLACK) as token:
        channels = await SlackClient().list_channels(token, limit=limit)

    return [SlackChannelRead.model_validate(channel) for channel in channels]


@router.get("/notion/pages", summary="Search Notion pages")
async def search_notion_pages(
    membership: CurrentMembership,
    session: SessionDep,
    redis: RedisDep,
    registry: RegistryDep,
    settings: SettingsDep,
    query: Annotated[str, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
) -> list[NotionPageRead]:
    """Pages the user shared with this integration when they connected it.

    An empty query returns their picker selection rather than the whole
    workspace — Notion scopes search to what was explicitly shared, which is a
    property worth not defeating.
    """
    service = _service(session, redis, registry, settings)

    async with service.using(membership.organization_id, Provider.NOTION) as token:
        pages = await NotionClient().search_pages(token, query=query, limit=limit)

    return [NotionPageRead.model_validate(page) for page in pages]


@router.get("/github/repositories", summary="List GitHub repositories")
async def list_github_repositories(
    membership: CurrentMembership,
    session: SessionDep,
    redis: RedisDep,
    registry: RegistryDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=30)] = 10,
) -> list[GitHubRepositoryRead]:
    """Public repositories for the connected account.

    Public *only*, and deliberately: M14 requests `read:user` and no repository
    scope, because GitHub's classic OAuth has no read-only grant for private code
    — `repo` would mean write access to everything the user can reach. See
    `integrations/github/oauth.py`.
    """
    service = _service(session, redis, registry, settings)

    async with service.using(membership.organization_id, Provider.GITHUB) as token:
        repositories = await GitHubClient().list_repositories(token, limit=limit)

    return [GitHubRepositoryRead.model_validate(repository) for repository in repositories]


@router.get("/stripe/charges", summary="List recent Stripe charges")
async def list_stripe_charges(
    membership: CurrentMembership,
    session: SessionDep,
    redis: RedisDep,
    registry: RegistryDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
) -> list[StripeChargeRead]:
    """Recent charges on the connected account. Read-only, structurally.

    The credential behind this call is a live Stripe secret key for somebody
    else's business, bounded by the `read_only` scope requested at connect time
    *and* by `StripeClient` having no method that is not a GET.
    """
    service = _service(session, redis, registry, settings)

    async with service.using(membership.organization_id, Provider.STRIPE) as token:
        charges = await StripeClient().list_charges(token, limit=limit)

    return [StripeChargeRead.model_validate(charge) for charge in charges]


@router.get("/gmail/messages", summary="List recent email")
async def list_gmail_messages(
    membership: CurrentMembership,
    session: SessionDep,
    redis: RedisDep,
    registry: RegistryDep,
    settings: SettingsDep,
    query: Annotated[str, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=10)] = 5,
) -> list[EmailMessageRead]:
    """Recent messages from the connected mailbox.

    `limit` caps at 10 rather than the 50 the other listings allow, because Gmail
    returns message *ids* and each one costs a second request — see
    `integrations/gmail/client.py`. The bound is a request budget, not a page size.
    """
    service = _service(session, redis, registry, settings)

    async with service.using(membership.organization_id, Provider.GMAIL) as token:
        messages = await GmailClient().list_messages(token, query=query, limit=limit)

    return [EmailMessageRead.model_validate(message) for message in messages]
