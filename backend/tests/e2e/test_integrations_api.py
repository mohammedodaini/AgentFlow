"""/api/v1/integrations over HTTP (M11).

The whole connect flow, driven the way a browser drives it: ask for a consent URL,
follow it, come back to the callback with a `state` and a `code`.

The assertion that matters most is `test_the_callback_needs_no_authentication`.
Every other endpoint here requires a membership; that one *cannot*, because it is
reached by Google redirecting the user's browser — no `Authorization` header
survives that trip. A test that quietly sent auth headers to the callback would
pass while the real flow was broken, and the breakage would only appear the first
time somebody clicked through a genuine consent screen.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret
from app.integrations import OAuthRegistry
from app.integrations.offline import OfflineOAuthProvider
from app.models.integration import IntegrationStatus, Provider
from tests.e2e.test_search_api import register

CALENDAR = Provider.GOOGLE_CALENDAR.value


def client_registry(client: AsyncClient) -> OAuthRegistry:
    """Reach the *same* provider instance the application is using.

    `OfflineOAuthProvider` is stateful — it holds the codes it issued and the
    refresh tokens it still considers live — so revoking a credential the way a
    user would means reaching the instance `lifespan()` built, not a new one.
    """
    transport = client._transport  # noqa: SLF001 — the only handle on the app under test
    assert isinstance(transport, ASGITransport)
    app = transport.app
    assert isinstance(app, FastAPI)
    registry: OAuthRegistry = app.state.oauth_registry
    return registry


async def authorize_url(client: AsyncClient, headers: dict[str, str]) -> str:
    response = await client.get(f"/api/v1/integrations/{CALENDAR}/connect", headers=headers)
    assert response.status_code == HTTPStatus.OK, response.text
    url: str = response.json()["authorize_url"]
    return url


async def connect(client: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    """Complete a full connect flow and return the integration."""
    query = parse_qs(urlparse(await authorize_url(client, headers)).query)

    response = await client.get(
        f"/api/v1/integrations/{CALENDAR}/callback",
        params={"state": query["state"][0], "code": query["code"][0]},
    )
    assert response.status_code == HTTPStatus.OK, response.text
    body: dict[str, Any] = response.json()
    return body


# --------------------------------------------------------------------------
# the flow
# --------------------------------------------------------------------------


async def test_connecting_an_account_end_to_end(client: AsyncClient) -> None:
    """The milestone over HTTP."""
    headers = await register(client)

    integration = await connect(client, headers)

    assert integration["provider"] == CALENDAR
    assert integration["status"] == IntegrationStatus.ACTIVE
    assert integration["external_account_id"] == "ada@example.test"
    assert integration["scopes"]


async def test_the_callback_needs_no_authentication(client: AsyncClient) -> None:
    """The one deliberately unauthenticated endpoint in the application.

    Google redirects the user's *browser* here, so no header we issued survives.
    `state` is the only credential the request can carry — which is exactly why it
    is unguessable, single-use, expiring, and the carrier of the tenant binding.

    Note the absence of `headers=` on the callback call below. That absence is the
    test.
    """
    headers = await register(client)
    query = parse_qs(urlparse(await authorize_url(client, headers)).query)

    response = await client.get(
        f"/api/v1/integrations/{CALENDAR}/callback",
        params={"state": query["state"][0], "code": query["code"][0]},
    )

    assert response.status_code == HTTPStatus.OK, response.text


async def test_a_response_never_contains_a_token(client: AsyncClient) -> None:
    """`IntegrationRead` has no token field to fill in, so publishing one would
    take somebody adding both a field and a line. The ciphertext is withheld too:
    it is not directly usable, but publishing it hands an attacker unlimited
    offline attempts against a value whose plaintext is a credential."""
    headers = await register(client)

    integration = await connect(client, headers)

    body = str(integration)
    assert "offline-access-" not in body
    assert "offline-refresh-" not in body
    assert "gAAAAA" not in body, "Fernet ciphertext must not be published either"
    assert set(integration) == {
        "id",
        "provider",
        "status",
        "external_account_id",
        "scopes",
        "created_at",
    }


# --------------------------------------------------------------------------
# the state parameter
# --------------------------------------------------------------------------


async def test_a_forged_callback_is_rejected(client: AsyncClient) -> None:
    """Login CSRF: without this check an attacker sends a victim a callback URL
    carrying the attacker's own code, and the attacker's Google account becomes the
    one connected to the victim's organization."""
    response = await client.get(
        f"/api/v1/integrations/{CALENDAR}/callback",
        params={"state": "invented", "code": "whatever"},
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_a_callback_cannot_be_replayed(client: AsyncClient) -> None:
    """A callback URL sits in browser history, in proxy logs and in `Referer`
    headers."""
    headers = await register(client)
    query = parse_qs(urlparse(await authorize_url(client, headers)).query)
    params = {"state": query["state"][0], "code": query["code"][0]}

    first = await client.get(f"/api/v1/integrations/{CALENDAR}/callback", params=params)
    second = await client.get(f"/api/v1/integrations/{CALENDAR}/callback", params=params)

    assert first.status_code == HTTPStatus.OK
    assert second.status_code == HTTPStatus.UNAUTHORIZED


async def test_a_callback_without_a_state_is_a_validation_error(client: AsyncClient) -> None:
    """Required rather than optional. An optional state is a state somebody
    eventually omits."""
    response = await client.get(
        f"/api/v1/integrations/{CALENDAR}/callback", params={"code": "whatever"}
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


# --------------------------------------------------------------------------
# listing, disconnecting, tenancy
# --------------------------------------------------------------------------


async def test_listing_shows_connected_accounts(client: AsyncClient) -> None:
    headers = await register(client)
    await connect(client, headers)

    response = await client.get("/api/v1/integrations", headers=headers)

    assert [item["provider"] for item in response.json()] == [CALENDAR]


async def test_a_disconnected_integration_is_still_listed(client: AsyncClient) -> None:
    """The row survives for the audit trail. "Who connected our calendar, and when
    was it removed?" is a question a deleted row cannot answer."""
    headers = await register(client)
    integration = await connect(client, headers)

    await client.delete(f"/api/v1/integrations/{integration['id']}", headers=headers)

    listed = (await client.get("/api/v1/integrations", headers=headers)).json()
    assert [item["status"] for item in listed] == [IntegrationStatus.DISCONNECTED]


async def test_you_cannot_see_another_tenants_integrations(client: AsyncClient) -> None:
    ours = await register(client)
    theirs = await register(client)
    await connect(client, theirs)

    assert (await client.get("/api/v1/integrations", headers=ours)).json() == []


async def test_you_cannot_disconnect_another_tenants_integration(client: AsyncClient) -> None:
    """404 rather than 403 — a distinct status would turn this into an oracle for
    enumerating integration ids across organizations."""
    ours = await register(client)
    theirs = await register(client)
    integration = await connect(client, theirs)

    response = await client.delete(f"/api/v1/integrations/{integration['id']}", headers=ours)

    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_disconnecting_something_that_does_not_exist_is_a_404(client: AsyncClient) -> None:
    headers = await register(client)

    response = await client.delete(f"/api/v1/integrations/{uuid.uuid4()}", headers=headers)

    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_an_unimplemented_provider_is_refused(client: AsyncClient) -> None:
    """`Provider` declares seven values so a new integration is a deploy rather than
    a migration. M14 implemented five more of them; Google Drive is the one left,
    because nothing in this product reads a file from Drive. Asking for it must be a
    clear 404 rather than a redirect to an authorization server nobody configured.

    This test named Slack until M14 implemented it — which is the useful kind of
    test failure: the assertion was still right and the example had become wrong."""
    headers = await register(client)

    response = await client.get("/api/v1/integrations/google_drive/connect", headers=headers)

    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_listing_requires_authentication(client: AsyncClient) -> None:
    """Every endpoint here except the callback requires a membership."""
    response = await client.get("/api/v1/integrations")

    assert response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}


async def test_reading_events_without_a_connection_is_a_404(client: AsyncClient) -> None:
    """The error a user can act on: connect the account.

    The *successful* path is not exercised here, deliberately — it would reach
    `googleapis.com`. Its parsing is covered by `tests/unit/test_calendar_client.py`
    against canned payloads, and the live call is verified by hand at runtime. A
    test that quietly made a real network request would be the worst of the three
    options.
    """
    headers = await register(client)

    response = await client.get("/api/v1/integrations/google-calendar/events", headers=headers)

    assert response.status_code == HTTPStatus.NOT_FOUND


# --------------------------------------------------------------------------
# M14: five more providers, over HTTP
# --------------------------------------------------------------------------


async def connect_provider(
    client: AsyncClient, headers: dict[str, str], provider: str
) -> dict[str, Any]:
    """Complete a connect flow for any provider, the way a browser drives it."""
    response = await client.get(f"/api/v1/integrations/{provider}/connect", headers=headers)
    assert response.status_code == HTTPStatus.OK, response.text

    query = parse_qs(urlparse(response.json()["authorize_url"]).query)
    callback = await client.get(
        f"/api/v1/integrations/{provider}/callback",
        params={"state": query["state"][0], "code": query["code"][0]},
    )
    assert callback.status_code == HTTPStatus.OK, callback.text
    body: dict[str, Any] = callback.json()
    return body


async def test_every_supported_provider_can_be_connected(client: AsyncClient) -> None:
    """Six providers, one flow. The point of M11's seam, demonstrated rather than
    claimed — nothing in the service or the routes is provider-specific."""
    headers = await register(client)

    for provider in ("gmail", "slack", "notion", "github", "stripe"):
        integration = await connect_provider(client, headers, provider)
        assert integration["provider"] == provider
        assert integration["status"] == IntegrationStatus.ACTIVE.value


async def test_a_perpetual_credential_stays_active_after_connecting(
    client: AsyncClient,
) -> None:
    """**The M11 bug, as a user would have met it.**

    A Slack bot token has no expiry and no refresh token. Before M14's fix, the
    *first* use of one marked the integration REVOKED and answered 404 "This
    integration expired and cannot be refreshed. Reconnect it." — on a credential
    issued seconds earlier — after which reconnecting produced another credential of
    the same shape and the same result.

    This asserts through HTTP that connecting leaves an ACTIVE integration whose
    token the service will hand out. It deliberately does **not** call
    `/slack/channels`: that would go to the real slack.com, which an earlier draft
    of this test did — see `_no_outbound_network` in `tests/conftest.py`. What the
    outbound call does is covered by `test_provider_clients.py` against a
    `MockTransport`, and what the credential does is covered against real Postgres
    in `tests/integration/test_perpetual_credentials.py`.
    """
    headers = await register(client)
    await connect_provider(client, headers, "slack")

    listed = await client.get("/api/v1/integrations", headers=headers)
    statuses = {one["provider"]: one["status"] for one in listed.json()}

    assert statuses["slack"] == IntegrationStatus.ACTIVE.value


async def test_reading_a_provider_without_a_connection_is_a_404(client: AsyncClient) -> None:
    """The error a user can act on: connect the account. One assertion per new
    endpoint, because a route that 500s on a missing integration is a route nobody
    can use before they have connected anything — which is everybody, once."""
    headers = await register(client)

    for path in (
        "/api/v1/integrations/slack/channels",
        "/api/v1/integrations/notion/pages",
        "/api/v1/integrations/github/repositories",
        "/api/v1/integrations/stripe/charges",
        "/api/v1/integrations/gmail/messages",
    ):
        response = await client.get(path, headers=headers)
        assert response.status_code == HTTPStatus.NOT_FOUND, path


async def test_the_new_endpoints_all_require_authentication(client: AsyncClient) -> None:
    """Only the callback may be unauthenticated, and it is the only one that is."""
    for path in (
        "/api/v1/integrations/providers",
        "/api/v1/integrations/slack/channels",
        "/api/v1/integrations/notion/pages",
        "/api/v1/integrations/github/repositories",
        "/api/v1/integrations/stripe/charges",
        "/api/v1/integrations/gmail/messages",
    ):
        response = await client.get(path)
        assert response.status_code in {
            HTTPStatus.UNAUTHORIZED,
            HTTPStatus.FORBIDDEN,
        }, path


async def test_the_providers_listing_describes_what_can_be_connected(
    client: AsyncClient,
) -> None:
    """Derived from the registry, not from a constant, so it reflects what is
    *configured*. A hard-coded list in the UI drifts the moment an operator sets a
    variable, and the failure is a button that leads to a 404."""
    headers = await register(client)

    response = await client.get("/api/v1/integrations/providers", headers=headers)

    assert response.status_code == HTTPStatus.OK
    listed = {entry["provider"] for entry in response.json()}
    assert listed == {"gmail", "google_calendar", "slack", "notion", "github", "stripe"}
    assert "google_drive" not in listed


async def test_the_providers_listing_names_the_scopes(client: AsyncClient) -> None:
    """What connecting will ask permission for, so a user can read it before they
    click rather than on Google's consent screen."""
    headers = await register(client)

    response = await client.get("/api/v1/integrations/providers", headers=headers)
    scopes = {entry["provider"]: entry["scopes"] for entry in response.json()}

    assert scopes["github"] == ["read:user"], "no repository scope — see github/oauth.py"
    assert scopes["stripe"] == ["read_only"]
    assert scopes["notion"] == [], "Notion grants access per page, not per scope"


async def test_a_slack_connection_stores_no_refresh_token(client: AsyncClient) -> None:
    """Visible from the API only as a connection that keeps working; asserted here
    through the flow that would otherwise have marked it revoked."""
    headers = await register(client)

    integration = await connect_provider(client, headers, "slack")

    assert integration["status"] == IntegrationStatus.ACTIVE.value
    assert "access_token" not in integration
    assert "refresh_token" not in integration


async def test_disconnecting_a_new_provider_works(client: AsyncClient) -> None:
    headers = await register(client)
    integration = await connect_provider(client, headers, "notion")

    response = await client.delete(f"/api/v1/integrations/{integration['id']}", headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == IntegrationStatus.DISCONNECTED.value


async def test_a_revocation_survives_the_request_that_discovered_it(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """**Found with curl, invisible to every test that came before it (M14).**

    When a provider refuses a credential, `IntegrationService._mark_revoked` records
    it and the caller then raises `NotFoundError` so the user is told to reconnect.
    Under M11 that record was a *flush* — and `get_db` rolls the session back on any
    exception, which is the whole point of session-per-request. So the very failure
    that prompted the write discarded it.

    The API said "access was revoked, reconnect it" while the row stayed ACTIVE. The
    integrations page kept showing a working connection, and every subsequent call
    rediscovered the same thing. Forever.

    Every existing test missed it because they call the service directly and assert
    inside the same transaction, where a flush *is* visible. Only an HTTP round trip
    shows the difference, which is why this test lives here and not beside the
    others.
    """
    headers = await register(client)
    await connect(client, headers)

    # Revoke from the "user's account page", the way the offline authorization
    # server models it — see `OfflineOAuthProvider.revoke`.
    integration = (await client.get("/api/v1/integrations", headers=headers)).json()[0]
    token = (
        await db_session.execute(
            text("SELECT refresh_token, id FROM oauth_tokens WHERE integration_id = :id"),
            {"id": integration["id"]},
        )
    ).one()
    provider = client_registry(client).get(Provider.GOOGLE_CALENDAR)
    assert isinstance(provider, OfflineOAuthProvider)
    provider.revoke(decrypt_secret(token[0]))

    # Age the access token so the next call has to refresh, which is where the
    # provider gets to say no.
    await db_session.execute(
        text("UPDATE oauth_tokens SET expires_at = now() - interval '1 hour' WHERE id = :id"),
        {"id": token[1]},
    )
    # **Required, and the network guard is what found it.** The HTTP request shares
    # this session (see the `app` fixture), so it shares the identity map — and a
    # raw UPDATE does not touch objects already loaded in it. Without this the
    # request read a stale `expires_at`, decided no refresh was needed, and went
    # straight out to googleapis.com. The test passed or failed depending on what
    # had run before it.
    db_session.expire_all()

    refused = await client.get("/api/v1/integrations/google-calendar/events", headers=headers)
    assert refused.status_code == HTTPStatus.NOT_FOUND
    assert "revoked" in refused.json()["error"]["message"].lower()

    # The assertion the bug was hiding: a *second, separate* request must agree.
    listed = (await client.get("/api/v1/integrations", headers=headers)).json()
    assert listed[0]["status"] == IntegrationStatus.REVOKED.value
