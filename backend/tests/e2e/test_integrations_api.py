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

from httpx import AsyncClient

from app.models.integration import IntegrationStatus, Provider
from tests.e2e.test_search_api import register

CALENDAR = Provider.GOOGLE_CALENDAR.value


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
    """`Provider` declares seven values so the *second* integration is a deploy
    rather than a migration. Only one is built, so asking for Slack must be a clear
    404 rather than a redirect to an authorization server nobody configured."""
    headers = await register(client)

    response = await client.get("/api/v1/integrations/slack/connect", headers=headers)

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
