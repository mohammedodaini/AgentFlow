"""The OAuth flow against real Postgres and real Redis (M11).

`test_oauth.py` covers the primitives. This covers the service that puts them
together — and the two properties that separate a working integration from a
security incident:

**The `state` check.** The callback is an unauthenticated request from a browser.
If `state` were guessable, or optional, or reusable, an attacker could send a
victim a crafted callback URL and have *their* Google account connected to the
victim's organization. The agent would then read the attacker's calendar, and from
M12 write to it. These tests are the defence.

**Keeping the refresh token.** Google returns none on a refresh. Code that writes
the grant back wholesale nulls a long-lived credential, and everything works for
an hour afterwards — so a test that only checked "refresh succeeded" would pass.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError, NotFoundError
from app.core.security import decrypt_secret
from app.integrations import OAuthRegistry
from app.integrations.offline import OfflineOAuthProvider
from app.models.integration import IntegrationStatus, Provider
from app.services.integration_service import STATE_PREFIX, IntegrationService
from tests.factories import make_org_with_owner
from tests.unit.test_oauth import code_from

CALENDAR = Provider.GOOGLE_CALENDAR


@pytest.fixture
def provider() -> OfflineOAuthProvider:
    """One authorization server per test, so revocations cannot leak between
    them."""
    return OfflineOAuthProvider(CALENDAR.value, ["calendar.readonly"])


@pytest.fixture
def service(
    db_session: AsyncSession, redis_client: Redis, provider: OfflineOAuthProvider
) -> IntegrationService:
    return IntegrationService(
        db_session, redis_client, OAuthRegistry({CALENDAR: provider}), get_settings()
    )


async def connect(
    service: IntegrationService,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> None:
    """Drive a full connect flow the way a browser would."""
    pending = await service.begin_connect(organization_id, user_id, CALENDAR)
    await service.complete_callback(
        CALENDAR, state=pending.state, code=code_from(pending.authorize_url)
    )


async def expire(db_session: AsyncSession, service: IntegrationService, org: uuid.UUID) -> None:
    """Age the stored access token past its expiry."""
    integration, _ = await service.get_fresh_token(org, CALENDAR)
    assert integration.tokens is not None
    integration.tokens.expires_at = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.flush()


# --------------------------------------------------------------------------
# connecting
# --------------------------------------------------------------------------


async def test_connecting_stores_an_encrypted_credential(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    """The milestone in one test: a completed flow leaves a usable credential that
    is not readable from the row."""
    organization, owner, _ = await make_org_with_owner(db_session)

    await connect(service, organization.id, owner.id)

    integration, access_token = await service.get_fresh_token(organization.id, CALENDAR)
    assert integration.status is IntegrationStatus.ACTIVE
    assert access_token.startswith("offline-access-")

    token = integration.tokens
    assert token is not None
    # The stored value is ciphertext; the plaintext is recoverable only through
    # the key. A `SELECT *` yields nothing usable.
    assert token.access_token != access_token
    assert decrypt_secret(token.access_token) == access_token


async def test_the_granted_scopes_are_recorded(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    """From the token response, never from what we asked for."""
    organization, owner, _ = await make_org_with_owner(db_session)

    await connect(service, organization.id, owner.id)

    integration, _ = await service.get_fresh_token(organization.id, CALENDAR)
    assert integration.scopes == ["calendar.readonly"]
    assert integration.external_account_id == "ada@example.test"


async def test_reconnecting_reuses_the_row(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    """The partial unique index allows one active row per provider, so a reconnect
    must reuse it — and reusing preserves the id, so anything already referring to
    this integration keeps working."""
    organization, owner, _ = await make_org_with_owner(db_session)

    await connect(service, organization.id, owner.id)
    first, _ = await service.get_fresh_token(organization.id, CALENDAR)
    first_id = first.id

    await connect(service, organization.id, owner.id)
    second, _ = await service.get_fresh_token(organization.id, CALENDAR)

    assert second.id == first_id
    assert len(await service.list_for_organization(organization.id)) == 1


# --------------------------------------------------------------------------
# the state parameter — the security model
# --------------------------------------------------------------------------


async def test_a_forged_callback_is_refused(service: IntegrationService) -> None:
    """The attack this whole mechanism exists to stop.

    Without the check, an attacker sends a victim a callback URL carrying the
    attacker's own authorization code, and the attacker's Google account becomes
    the one connected to the victim's organization.
    """
    with pytest.raises(AuthenticationError):
        await service.complete_callback(CALENDAR, state="invented", code="whatever")


async def test_a_state_works_exactly_once(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    """Consumed with `GETDEL`, so two callbacks arriving together — a
    double-clicked consent screen, a browser retrying — cannot both proceed."""
    organization, owner, _ = await make_org_with_owner(db_session)
    pending = await service.begin_connect(organization.id, owner.id, CALENDAR)
    code = code_from(pending.authorize_url)

    await service.complete_callback(CALENDAR, state=pending.state, code=code)

    with pytest.raises(AuthenticationError):
        await service.complete_callback(CALENDAR, state=pending.state, code=code)


async def test_a_state_issued_for_one_provider_cannot_be_used_for_another(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    """Nothing good explains a mismatch, and the message is identical to every
    other rejection so an attacker learns nothing about which states exist."""
    organization, owner, _ = await make_org_with_owner(db_session)
    pending = await service.begin_connect(organization.id, owner.id, CALENDAR)

    with pytest.raises(AuthenticationError):
        await service.complete_callback(Provider.SLACK, state=pending.state, code="c")


async def test_the_state_decides_the_owning_organization(
    db_session: AsyncSession, service: IntegrationService, redis_client: Redis
) -> None:
    """The callback carries no auth headers, so the binding *must* come from the
    state — and this asserts it is what decides the owning tenant."""
    organization, owner, _ = await make_org_with_owner(db_session)
    other, _, _ = await make_org_with_owner(db_session)

    pending = await service.begin_connect(organization.id, owner.id, CALENDAR)
    await service.complete_callback(
        CALENDAR, state=pending.state, code=code_from(pending.authorize_url)
    )

    assert len(await service.list_for_organization(organization.id)) == 1
    assert await service.list_for_organization(other.id) == []
    # Gone from Redis, not merely marked used.
    assert await redis_client.get(f"{STATE_PREFIX}{pending.state}") is None


async def test_a_state_expires(
    db_session: AsyncSession, service: IntegrationService, redis_client: Redis
) -> None:
    """A TTL is set, so an abandoned attempt cannot be resumed from a URL somebody
    later finds in a browser history or a proxy log."""
    organization, owner, _ = await make_org_with_owner(db_session)

    pending = await service.begin_connect(organization.id, owner.id, CALENDAR)

    ttl = await redis_client.ttl(f"{STATE_PREFIX}{pending.state}")
    assert 0 < ttl <= Settings().oauth_state_ttl_seconds


# --------------------------------------------------------------------------
# refresh — where the expensive bug lives
# --------------------------------------------------------------------------


async def test_an_expired_token_is_refreshed_before_use(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    """Checked before the call rather than after a 401, because expired and
    revoked are indistinguishable at the HTTP layer and only one of them should
    stop the integration."""
    organization, owner, _ = await make_org_with_owner(db_session)
    await connect(service, organization.id, owner.id)

    _, first_token = await service.get_fresh_token(organization.id, CALENDAR)
    await expire(db_session, service, organization.id)

    _, second_token = await service.get_fresh_token(organization.id, CALENDAR)

    assert second_token != first_token


async def test_refreshing_keeps_the_refresh_token(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    """**The most consequential assertion in this milestone.**

    Google returns no `refresh_token` on a refresh. Code that assigns the grant
    wholesale writes NULL over a long-lived credential — and everything keeps
    working for up to an hour, so a test that only checked "refresh succeeded"
    would pass while the integration was already doomed.
    """
    organization, owner, _ = await make_org_with_owner(db_session)
    await connect(service, organization.id, owner.id)

    integration, _ = await service.get_fresh_token(organization.id, CALENDAR)
    assert integration.tokens is not None
    original_refresh = integration.tokens.refresh_token
    await expire(db_session, service, organization.id)

    refreshed, _ = await service.get_fresh_token(organization.id, CALENDAR)

    assert refreshed.tokens is not None
    assert refreshed.tokens.refresh_token == original_refresh


async def test_a_revoked_credential_marks_the_integration_and_stops(
    db_session: AsyncSession, service: IntegrationService, provider: OfflineOAuthProvider
) -> None:
    """A user revoked access from their Google account page. A normal event on
    their side, and no retry helps — so the row records it rather than letting
    every caller keep trying forever."""
    organization, owner, _ = await make_org_with_owner(db_session)
    await connect(service, organization.id, owner.id)

    integration, _ = await service.get_fresh_token(organization.id, CALENDAR)
    assert integration.tokens is not None
    provider.revoke(decrypt_secret(integration.tokens.refresh_token or ""))
    await expire(db_session, service, organization.id)

    with pytest.raises(NotFoundError, match="revoked"):
        await service.get_fresh_token(organization.id, CALENDAR)

    listed = await service.list_for_organization(organization.id)
    assert listed[0].status is IntegrationStatus.REVOKED
    # The dead credential is destroyed rather than kept beside the status: nobody
    # legitimate can use it again, so retaining it stores only risk.
    assert listed[0].tokens is None


async def test_an_expired_token_with_nothing_to_refresh_is_terminal(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    """Some providers never issue a refresh token. Once the access token expires
    there is no way forward, and recording that beats every caller rediscovering
    it."""
    organization, owner, _ = await make_org_with_owner(db_session)
    await connect(service, organization.id, owner.id)

    integration, _ = await service.get_fresh_token(organization.id, CALENDAR)
    assert integration.tokens is not None
    integration.tokens.refresh_token = None
    integration.tokens.expires_at = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.flush()

    with pytest.raises(NotFoundError):
        await service.get_fresh_token(organization.id, CALENDAR)

    listed = await service.list_for_organization(organization.id)
    assert listed[0].status is IntegrationStatus.REVOKED


# --------------------------------------------------------------------------
# disconnecting and tenancy
# --------------------------------------------------------------------------


async def test_disconnecting_keeps_the_row_and_destroys_the_credential(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    """ "Who connected our calendar, and when was it removed?" is an audit question
    a deleted row cannot answer. The token is a liability, and it goes."""
    organization, owner, _ = await make_org_with_owner(db_session)
    await connect(service, organization.id, owner.id)
    integration, _ = await service.get_fresh_token(organization.id, CALENDAR)

    disconnected = await service.disconnect(organization.id, integration.id)

    assert disconnected.status is IntegrationStatus.DISCONNECTED
    assert disconnected.tokens is None

    with pytest.raises(NotFoundError):
        await service.get_fresh_token(organization.id, CALENDAR)


async def test_reconnecting_after_disconnect_opens_a_new_row(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    """A disconnected row is history. Resurrecting it would overwrite the record of
    when the previous connection ended."""
    organization, owner, _ = await make_org_with_owner(db_session)
    await connect(service, organization.id, owner.id)
    first, _ = await service.get_fresh_token(organization.id, CALENDAR)
    await service.disconnect(organization.id, first.id)

    await connect(service, organization.id, owner.id)
    second, _ = await service.get_fresh_token(organization.id, CALENDAR)

    assert second.id != first.id
    assert len(await service.list_for_organization(organization.id)) == 2


async def test_another_tenants_integration_is_not_found(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    organization, owner, _ = await make_org_with_owner(db_session)
    other, _, _ = await make_org_with_owner(db_session)
    await connect(service, organization.id, owner.id)
    integration, _ = await service.get_fresh_token(organization.id, CALENDAR)

    with pytest.raises(NotFoundError):
        await service.disconnect(other.id, integration.id)


async def test_an_organization_with_no_integration_gets_a_clear_error(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    organization, _, _ = await make_org_with_owner(db_session)

    with pytest.raises(NotFoundError, match="No active google_calendar"):
        await service.get_fresh_token(organization.id, CALENDAR)
