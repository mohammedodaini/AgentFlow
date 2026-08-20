"""Credentials that never expire, and the M11 assumption they broke (M14).

M11 built the token store around Google, where every credential expires and every
credential can be refreshed. Three of the five providers M14 adds — Slack, Notion
and GitHub — issue a token with **no expiry and no refresh token**, valid until
somebody uninstalls the app.

Under M11's rules that combination was fatal on first use:

    needs_refresh()          → True   (expires_at IS NULL was "expired")
    token.refresh_token      → None
    → _mark_revoked(...)     → status = REVOKED, credential destroyed

So a user connected Slack, the connection succeeded, and the very first call
marked it broken and told them to reconnect — which produced another credential of
exactly the same shape, and the same result. Forever.

The bug was invisible to every test in the suite because `OfflineOAuthProvider`
always behaved like Google. A test double that can only produce the shape the code
already handles is a test double that certifies the bug, so `perpetual=True` came
first and these tests came after it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import NotFoundError
from app.core.security import decrypt_secret
from app.integrations import PERPETUAL_PROVIDERS, OAuthRegistry
from app.integrations.base import OAuthRevokedError
from app.integrations.offline import OfflineOAuthProvider
from app.models.integration import IntegrationStatus, Provider
from app.models.oauth_token import OAuthToken
from app.services.integration_service import IntegrationService
from tests.factories import make_org_with_owner
from tests.unit.test_oauth import code_from

SLACK = Provider.SLACK


@pytest.fixture
def provider() -> OfflineOAuthProvider:
    """A Slack-shaped authorization server: no expiry, no refresh token."""
    return OfflineOAuthProvider(SLACK.value, ["channels:read"], perpetual=True)


@pytest.fixture
def service(
    db_session: AsyncSession, redis_client: Redis, provider: OfflineOAuthProvider
) -> IntegrationService:
    return IntegrationService(
        db_session, redis_client, OAuthRegistry({SLACK: provider}), get_settings()
    )


async def connect(service: IntegrationService, organization_id: uuid.UUID) -> None:
    pending = await service.begin_connect(organization_id, None, SLACK)
    await service.complete_callback(
        SLACK, state=pending.state, code=code_from(pending.authorize_url)
    )


# --------------------------------------------------------------------------
# the shape itself
# --------------------------------------------------------------------------


def test_a_token_with_neither_expiry_nor_refresh_is_perpetual() -> None:
    """The two conditions are checked together on purpose.

    `expires_at IS NULL` alone would also match a Google response that arrived
    malformed, and a refresh token alone is what makes renewal possible — so it is
    precisely the *absence of both* that means "nothing to renew, and nothing said
    this would stop working".
    """
    token = OAuthToken(integration_id=uuid.uuid4(), access_token="x")

    assert token.is_perpetual is True
    assert token.needs_refresh() is False


def test_a_token_with_no_expiry_but_a_refresh_token_is_still_refreshed() -> None:
    """M11's safe direction, preserved. One wasted HTTP call against a user-facing
    failure is not a close trade — this branch is Google answering oddly, not a
    provider saying "this never expires"."""
    token = OAuthToken(integration_id=uuid.uuid4(), access_token="x", refresh_token="r")

    assert token.is_perpetual is False
    assert token.needs_refresh() is True


def test_a_stated_expiry_still_governs() -> None:
    token = OAuthToken(
        integration_id=uuid.uuid4(),
        access_token="x",
        refresh_token="r",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    assert token.needs_refresh() is False
    assert token.needs_refresh(now=datetime.now(UTC) + timedelta(hours=2)) is True


def test_the_registry_knows_which_providers_are_perpetual() -> None:
    """Slack, Notion and GitHub. Gmail, Calendar and Stripe all issue refresh
    tokens, so they keep the Google-shaped path."""
    assert {Provider.SLACK, Provider.NOTION, Provider.GITHUB} == PERPETUAL_PROVIDERS


# --------------------------------------------------------------------------
# through the real service
# --------------------------------------------------------------------------


async def test_a_perpetual_credential_survives_its_first_use(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    """**The regression test for the whole milestone's worst bug.**

    Before the fix this call marked the integration REVOKED and raised
    `NotFoundError("This integration expired and cannot be refreshed")` — seconds
    after a successful connect, on a credential that was perfectly valid.
    """
    organization, _, _ = await make_org_with_owner(db_session)
    await connect(service, organization.id)

    integration, access_token = await service.get_fresh_token(organization.id, SLACK)

    assert integration.status is IntegrationStatus.ACTIVE
    assert access_token.startswith("offline-access-")
    assert integration.tokens is not None
    assert integration.tokens.refresh_token is None
    assert integration.tokens.expires_at is None


async def test_a_perpetual_credential_survives_repeated_use(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    """The same token comes back every time, and nothing is refreshed.

    Asserted because "works once" and "works" are different claims, and the failure
    this replaces appeared on the *first* call rather than the tenth.
    """
    organization, _, _ = await make_org_with_owner(db_session)
    await connect(service, organization.id)

    first = await service.get_fresh_token(organization.id, SLACK)
    second = await service.get_fresh_token(organization.id, SLACK)

    assert first[1] == second[1]
    assert second[0].status is IntegrationStatus.ACTIVE


async def test_a_perpetual_credential_is_never_sent_to_the_refresh_endpoint(
    db_session: AsyncSession, service: IntegrationService, provider: OfflineOAuthProvider
) -> None:
    """The offline provider refuses to refresh, as Notion's real endpoint does.
    Reaching it at all would mean the fix was in the wrong place."""
    organization, _, _ = await make_org_with_owner(db_session)
    await connect(service, organization.id)

    with pytest.raises(OAuthRevokedError):
        await provider.refresh("anything")

    _, access_token = await service.get_fresh_token(organization.id, SLACK)
    assert access_token


# --------------------------------------------------------------------------
# noticing that one has died
# --------------------------------------------------------------------------


async def test_a_rejected_perpetual_credential_is_recorded_as_revoked(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    """**The second half of the same bug.**

    Under M11, `REVOKED` had exactly one writer — the refresh path. A perpetual
    credential is never refreshed, so when the provider started rejecting it,
    nothing recorded that: the call failed with a 502, the row stayed ACTIVE, the
    integrations page kept saying "connected", and every later call failed
    identically. Forever, and invisibly.

    `using()` makes getting a token and noticing it died the same operation.
    """
    organization, _, _ = await make_org_with_owner(db_session)
    await connect(service, organization.id)

    with pytest.raises(NotFoundError) as raised:
        async with service.using(organization.id, SLACK):
            # What `SlackClient` raises for `{"ok": false, "error": "invalid_auth"}`.
            message = "Slack rejected the workspace credential."
            raise OAuthRevokedError(message)

    assert "Reconnect" in raised.value.message

    # The row survives — "who connected this, and when did it break?" is an audit
    # question a deleted row cannot answer — but the credential is destroyed and the
    # status now tells the user the one thing they can act on.
    integrations = await service.list_for_organization(organization.id)
    assert [one.status for one in integrations] == [IntegrationStatus.REVOKED]
    assert integrations[0].tokens is None


async def test_a_successful_call_leaves_the_integration_alone(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    """The other side of `using()`: no exception, no state change."""
    organization, _, _ = await make_org_with_owner(db_session)
    await connect(service, organization.id)

    async with service.using(organization.id, SLACK) as token:
        assert token.startswith("offline-access-")

    integration, _ = await service.get_fresh_token(organization.id, SLACK)
    assert integration.status is IntegrationStatus.ACTIVE


async def test_an_ordinary_failure_does_not_revoke_anything(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    """Only `OAuthRevokedError` means the credential is dead. A timeout, a 500 or a
    rate limit must leave a working integration working — the alternative is an
    integration marked broken because the provider was busy for a minute."""
    organization, _, _ = await make_org_with_owner(db_session)
    await connect(service, organization.id)

    with pytest.raises(TimeoutError):
        async with service.using(organization.id, SLACK):
            raise TimeoutError

    integration, _ = await service.get_fresh_token(organization.id, SLACK)
    assert integration.status is IntegrationStatus.ACTIVE


async def test_the_stored_credential_is_still_encrypted(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    """Perpetual does not mean plaintext. A credential that never expires is the
    one it matters *most* to encrypt, because a leak of it never lapses."""
    organization, _, _ = await make_org_with_owner(db_session)
    await connect(service, organization.id)

    integration, access_token = await service.get_fresh_token(organization.id, SLACK)

    assert integration.tokens is not None
    assert integration.tokens.access_token != access_token
    assert decrypt_secret(integration.tokens.access_token) == access_token


async def test_an_unconfigured_provider_is_a_404_naming_the_variables(
    db_session: AsyncSession, service: IntegrationService
) -> None:
    """M11 raised `OAuthError` here, which maps to 502 — an *upstream* failure
    inviting a retry. Nothing was contacted and no retry sets an environment
    variable."""
    organization, _, _ = await make_org_with_owner(db_session)

    with pytest.raises(NotFoundError) as raised:
        await service.begin_connect(organization.id, None, Provider.NOTION)

    assert "NOTION_CLIENT_ID" in raised.value.message
