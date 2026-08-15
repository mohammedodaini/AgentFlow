"""/api/v1/approvals over HTTP (M12).

The whole loop as a client sees it: propose, find it in the inbox, decide, and get
the right status back when deciding twice.

The assertion worth the file is `test_proposing_never_creates_an_event`. Everything
else here checks that the machinery behaves; that one checks the *claim* — that the
endpoint which sounds like it schedules a meeting cannot schedule a meeting.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Any

import pytest
from httpx import AsyncClient

from app.agents.calendar import tools as calendar_tools
from app.models.approval import ApprovalStatus
from tests.e2e.test_search_api import register
from tests.integration.test_approvals import FakeCalendarClient

INSTRUCTION = "Schedule a design review on 2026-08-20 09:00"


@pytest.fixture(autouse=True)
def _stub_google(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the calendar client in one module — never `httpx` globally."""
    FakeCalendarClient.reset()
    monkeypatch.setattr(calendar_tools, "GoogleCalendarClient", FakeCalendarClient)


async def propose(
    client: AsyncClient, headers: dict[str, str], instruction: str = INSTRUCTION
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/agent-runs/calendar", headers=headers, json={"instruction": instruction}
    )
    assert response.status_code == HTTPStatus.OK, response.text
    body: dict[str, Any] = response.json()
    return body


# --------------------------------------------------------------------------
# proposing
# --------------------------------------------------------------------------


async def test_proposing_returns_a_paused_run_and_an_approval(client: AsyncClient) -> None:
    headers = await register(client)

    body = await propose(client, headers)

    assert body["status"] == "paused_for_approval"
    assert body["approval"]["status"] == ApprovalStatus.PENDING
    assert "design review" in body["approval"]["summary"]


async def test_proposing_never_creates_an_event(client: AsyncClient) -> None:
    """**The claim this milestone makes, asserted over HTTP.**

    The endpoint is named for scheduling and it cannot schedule: the executor is
    built only on the resume path, so there is no branch here that could be taken.
    """
    headers = await register(client)

    await propose(client, headers)

    assert FakeCalendarClient.created == []


async def test_the_action_is_published_so_a_human_can_check_it(client: AsyncClient) -> None:
    """`requested_action` is returned in full — the opposite of what `AgentRunRead`
    does with `checkpoint`, and right for the opposite reason: a person cannot
    meaningfully approve something they are not shown."""
    headers = await register(client)

    body = await propose(client, headers)
    action = body["approval"]["requested_action"]

    assert action["kind"] == "calendar.create_event"
    assert action["starts_at"] == "2026-08-20T09:00:00+00:00"


async def test_an_unparseable_instruction_asks_for_nothing(client: AsyncClient) -> None:
    headers = await register(client)

    body = await propose(client, headers, "Schedule something tomorrow afternoon")

    assert body["status"] == "succeeded"
    assert body["approval"] is None
    assert "date and time" in (body["message"] or "")
    assert (await client.get("/api/v1/approvals", headers=headers)).json() == []


# --------------------------------------------------------------------------
# the inbox
# --------------------------------------------------------------------------


async def test_the_inbox_lists_what_is_waiting(client: AsyncClient) -> None:
    headers = await register(client)
    body = await propose(client, headers)

    inbox = (await client.get("/api/v1/approvals", headers=headers)).json()

    assert [item["id"] for item in inbox] == [body["approval"]["id"]]


async def test_the_inbox_is_scoped_to_the_tenant(client: AsyncClient) -> None:
    ours = await register(client)
    theirs = await register(client)
    await propose(client, theirs)

    assert (await client.get("/api/v1/approvals", headers=ours)).json() == []


async def test_another_tenants_approval_is_not_found(client: AsyncClient) -> None:
    """404 rather than 403 — a distinct status would confirm the id exists
    somewhere."""
    ours = await register(client)
    theirs = await register(client)
    body = await propose(client, theirs)

    response = await client.get(f"/api/v1/approvals/{body['approval']['id']}", headers=ours)

    assert response.status_code == HTTPStatus.NOT_FOUND


# --------------------------------------------------------------------------
# deciding
# --------------------------------------------------------------------------


async def test_rejecting_records_the_reason_and_empties_the_inbox(client: AsyncClient) -> None:
    headers = await register(client)
    body = await propose(client, headers)

    response = await client.post(
        f"/api/v1/approvals/{body['approval']['id']}/reject",
        headers=headers,
        json={"reason": "We already have that meeting."},
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == ApprovalStatus.REJECTED
    assert response.json()["reason"] == "We already have that meeting."
    assert (await client.get("/api/v1/approvals", headers=headers)).json() == []
    assert FakeCalendarClient.created == []


async def test_deciding_twice_is_a_conflict(client: AsyncClient) -> None:
    """409, not 200. The decision arrives from a browser and browsers retry — a
    client that lost the race is told the decision already happened rather than being
    quietly given a second one."""
    headers = await register(client)
    body = await propose(client, headers)
    approval_id = body["approval"]["id"]

    first = await client.post(f"/api/v1/approvals/{approval_id}/reject", headers=headers, json={})
    second = await client.post(f"/api/v1/approvals/{approval_id}/reject", headers=headers, json={})

    assert first.status_code == HTTPStatus.OK
    assert second.status_code == HTTPStatus.CONFLICT


async def test_approving_without_a_connected_calendar_says_so(client: AsyncClient) -> None:
    """Proposing needs no integration; executing does. The failure lands where
    somebody is trying to act, with an error naming the fix."""
    headers = await register(client)
    body = await propose(client, headers)

    response = await client.post(
        f"/api/v1/approvals/{body['approval']['id']}/approve", headers=headers, json={}
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert "google_calendar" in response.text


async def test_deciding_on_something_that_does_not_exist_is_a_404(client: AsyncClient) -> None:
    headers = await register(client)

    response = await client.post(
        f"/api/v1/approvals/{uuid.uuid4()}/reject", headers=headers, json={}
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_every_approval_endpoint_requires_authentication(client: AsyncClient) -> None:
    """Unlike M11's OAuth callback, nothing here is reachable without a membership:
    each of these either reads or authorises a side effect."""
    unauthorised = {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}

    assert (await client.get("/api/v1/approvals")).status_code in unauthorised
    assert (
        await client.post("/api/v1/agent-runs/calendar", json={"instruction": INSTRUCTION})
    ).status_code in unauthorised
    assert (
        await client.post(f"/api/v1/approvals/{uuid.uuid4()}/approve", json={})
    ).status_code in unauthorised


async def test_an_empty_instruction_is_rejected_at_the_boundary(client: AsyncClient) -> None:
    headers = await register(client)

    response = await client.post(
        "/api/v1/agent-runs/calendar", headers=headers, json={"instruction": ""}
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


# --------------------------------------------------------------------------
# the trace
# --------------------------------------------------------------------------


async def test_the_run_records_what_it_proposed(client: AsyncClient) -> None:
    """The trace is a client-facing surface (ADR-0012), and for an action awaiting
    approval it answers the question that matters most: what exactly was somebody
    asked to permit?"""
    headers = await register(client)
    body = await propose(client, headers)

    run = (await client.get(f"/api/v1/agent-runs/{body['agent_run_id']}", headers=headers)).json()

    assert run["status"] == "paused_for_approval"
    assert [step["node_name"] for step in run["steps"]] == ["plan", "propose"]
    assert "checkpoint" not in run, "the graph's internal state stays unpublished"
