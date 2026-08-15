"""Translating Google's calendar payloads into ours (M11).

The boundary rule — provider types never leak upward — is only worth having if
the translation is correct, and Google's event resource has three separate traps
in it. All three are asserted here against canned payloads, because the
interesting failures are in *parsing* rather than in HTTP.

A `MockTransport` rather than a patched `httpx.AsyncClient`, so only this client's
wire is replaced. Patching globally silences every HTTP call in the process,
including ones a test never meant to stub — and then a real request that should
have failed quietly passes.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.integrations.base import OAuthError, OAuthRevokedError
from app.integrations.google_calendar.client import GoogleCalendarClient

TOKEN = "ya29.test-access-token"  # noqa: S105 — synthetic


def client_returning(payload: dict[str, Any], *, status: int = 200) -> GoogleCalendarClient:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status, json=payload)

    return GoogleCalendarClient(transport=httpx.MockTransport(handler))


def event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "evt-1",
        "summary": "Stand-up",
        "start": {"dateTime": "2026-08-20T09:00:00+00:00"},
        "end": {"dateTime": "2026-08-20T09:15:00+00:00"},
        "htmlLink": "https://calendar.google.test/evt-1",
        "status": "confirmed",
    }
    return base | overrides


async def test_a_normal_event_is_translated() -> None:
    events = await client_returning({"items": [event()]}).list_events(TOKEN)

    assert len(events) == 1
    assert events[0].title == "Stand-up"
    assert events[0].starts_at is not None
    assert events[0].all_day is False
    assert events[0].url == "https://calendar.google.test/evt-1"


async def test_an_all_day_event_has_no_time_rather_than_midnight() -> None:
    """The trap worth the most.

    Google expresses an all-day event as a `date` with no time. Modelling that as
    midnight is a silent lie — an all-day event is not an event at 00:00 — and any
    availability check built on that assumption would be wrong in a way nobody
    would think to question.
    """
    payload = {"items": [event(start={"date": "2026-08-20"}, end={"date": "2026-08-21"})]}

    events = await client_returning(payload).list_events(TOKEN)

    assert events[0].all_day is True
    assert events[0].starts_at is None
    assert events[0].ends_at is None


async def test_a_cancelled_occurrence_is_dropped() -> None:
    """A cancelled occurrence still appears in the list, so a client syncing
    incrementally learns it was cancelled. We are not syncing — showing them would
    put meetings that are not happening in front of a user, and in front of a
    model."""
    payload = {"items": [event(), event(id="evt-2", status="cancelled")]}

    events = await client_returning(payload).list_events(TOKEN)

    assert [item.event_id for item in events] == ["evt-1"]


async def test_an_untitled_event_gets_a_readable_label() -> None:
    """Google omits `summary` entirely rather than sending an empty string, and ""
    renders as a blank row nobody can click."""
    payload = {"items": [{key: value for key, value in event().items() if key != "summary"}]}

    events = await client_returning(payload).list_events(TOKEN)

    assert events[0].title == "(no title)"


async def test_an_empty_calendar_is_not_an_error() -> None:
    assert await client_returning({}).list_events(TOKEN) == []


async def test_a_rejected_credential_is_reported_as_revoked() -> None:
    """The service refreshes *before* calling, so a token rejected here was minted
    seconds ago. That is not staleness — it is a dead credential, and the caller
    has to be able to tell the difference."""
    with pytest.raises(OAuthRevokedError):
        await client_returning({}, status=401).list_events(TOKEN)


async def test_a_provider_outage_is_retryable() -> None:
    """A 500 is not a revocation. Collapsing the two would either retry a dead
    credential forever or disconnect a working one over a blip."""
    with pytest.raises(OAuthError) as caught:
        await client_returning({}, status=500).list_events(TOKEN)

    assert not isinstance(caught.value, OAuthRevokedError)


async def test_a_network_failure_is_translated() -> None:
    """An `httpx.ConnectError` escaping this layer would surface as a 500
    mentioning neither Google nor the integration."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        message = "connection refused"
        raise httpx.ConnectError(message)

    client = GoogleCalendarClient(transport=httpx.MockTransport(handler))

    with pytest.raises(OAuthError):
        await client.list_events(TOKEN)


async def test_recurring_events_are_requested_already_expanded() -> None:
    """`singleEvents=true` expands a recurrence into occurrences, and
    `orderBy=startTime` is only legal alongside it — Google returns 400 otherwise,
    so the two travel together.

    Without expansion a weekly stand-up returns as *one* event carrying a
    recurrence rule, and every caller would need RFC 5545 expansion to answer
    "what is on my calendar on Thursday" — the only question anyone asks.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"items": []})

    await GoogleCalendarClient(transport=httpx.MockTransport(handler)).list_events(TOKEN)

    assert seen["singleEvents"] == "true"
    assert seen["orderBy"] == "startTime"
    assert "timeMin" in seen
