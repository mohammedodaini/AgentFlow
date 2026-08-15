"""Google Calendar API client — the only code that speaks Google's wire format.

Layer: integrations. Consumed by services and, from M12, by agent tools. Returns
**our** shapes, never a raw provider payload.

That rule earns its keep here more than anywhere else so far. A Google event
resource has forty-odd fields, three different ways to express a time, and a
`status` that can be `cancelled` on an event still present in the list. Letting
that dict travel upward would put Google's data model into our API responses, our
prompts and our tests — and a change on their side would then be a change
everywhere.

Read-only at M11; writing arrived at M12
----------------------------------------
M11 shipped `list_events` and nothing else, and said why: a create-event method
existing "ready for later" would mean the only thing between an agent and
somebody's diary was that no code path happened to call it yet.

M12 added `create_event`, and what changed is not this class — it is that
`approvals` now exists. The single caller lives in `app/agents/calendar/tools.py`
and is unreachable without a row a human decided on. The scope widened with it,
from `calendar.readonly` to `calendar.events`, which means every account connected
under M11 holds a read-only credential and must reconnect before a write succeeds
(`post_json` turns Google's 403 into exactly that instruction).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.integrations.base import BaseClient

EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

MAX_RESULTS = 50
"""A page of events. Bounded because an unbounded listing becomes a prompt nobody
budgeted for once an agent tool consumes this."""


@dataclass(frozen=True)
class CalendarEvent:
    """One event, reduced to what this product actually uses.

    Six fields out of Google's forty. Everything omitted is a field we would have
    to keep working, keep testing and keep in a schema — for no current caller.

    `starts_at` is `datetime | None` because Google expresses an all-day event as
    a `date` with no time at all. Modelling that as midnight would be a silent
    lie: an all-day event is not an event at 00:00, and an availability check
    built on that assumption would be wrong in a way nobody would question.
    """

    event_id: str
    title: str
    starts_at: datetime | None
    ends_at: datetime | None
    all_day: bool
    url: str | None


class GoogleCalendarClient(BaseClient):
    """Reads a connected calendar. Holds no credential of its own."""

    async def list_events(
        self, access_token: str, *, time_min: datetime | None = None, limit: int = MAX_RESULTS
    ) -> list[CalendarEvent]:
        """Upcoming events from the account's primary calendar.

        `singleEvents=true` expands recurring events into their occurrences.
        Without it a weekly stand-up comes back as *one* event carrying a
        recurrence rule, and every caller would have to implement RFC 5545
        expansion to answer "what is on my calendar on Thursday" — which is the
        only question anyone asks.

        `orderBy=startTime` is only legal *with* `singleEvents`, and Google
        returns 400 otherwise. The two travel together for that reason.
        """
        payload = await self.get_json(
            EVENTS_URL,
            access_token=access_token,
            params={
                "timeMin": (time_min or datetime.now(UTC)).isoformat(),
                "maxResults": min(limit, MAX_RESULTS),
                "singleEvents": "true",
                "orderBy": "startTime",
            },
        )

        return [
            _as_event(item)
            for item in payload.get("items", [])
            # A cancelled occurrence still appears in the list, so a client syncing
            # incrementally learns it was cancelled. We are not syncing; showing
            # them would put meetings that are not happening in front of a user,
            # and in front of a model.
            if item.get("status") != "cancelled"
        ]

    async def create_event(
        self,
        access_token: str,
        *,
        summary: str,
        starts_at: datetime,
        ends_at: datetime,
        description: str | None = None,
    ) -> CalendarEvent:
        """Put an event on the calendar. **M12, and only behind an approval.**

        M11 shipped this class read-only and said why: a write method existing
        "ready for later" would mean the only thing between an agent and somebody's
        diary was that no code path happened to call it. What changed is not this
        method — it is that `approvals` now exists, and the one caller
        (`app/agents/calendar/tools.py`) cannot reach it without a row a human
        decided on.

        Times are sent as ISO-8601 *with* an offset, which is why the parameters
        are `datetime` rather than `str`. Google accepts a naive string and
        interprets it in the calendar's default timezone — so a meeting proposed at
        09:00 UTC silently lands at 09:00 in Los Angeles, and the failure is a
        person missing a meeting rather than an error anybody sees.
        """
        body: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": starts_at.isoformat()},
            "end": {"dateTime": ends_at.isoformat()},
        }

        if description:
            body["description"] = description

        return _as_event(await self.post_json(EVENTS_URL, access_token=access_token, body=body))


def _as_event(item: dict[str, Any]) -> CalendarEvent:
    """Translate one Google event resource into ours."""
    start, start_all_day = _as_datetime(item.get("start", {}))
    end, _ = _as_datetime(item.get("end", {}))

    return CalendarEvent(
        event_id=str(item.get("id", "")),
        # Google omits `summary` entirely for an untitled event rather than
        # sending an empty string, and "" renders as a blank row nobody can click.
        title=str(item.get("summary") or "(no title)"),
        starts_at=start,
        ends_at=end,
        all_day=start_all_day,
        url=item.get("htmlLink"),
    )


def _as_datetime(marker: dict[str, Any]) -> tuple[datetime | None, bool]:
    """Read Google's `{"dateTime": ...}` or `{"date": ...}`, and say which it was.

    Returns `(None, True)` for an all-day event rather than inventing a time. See
    `CalendarEvent.starts_at`.
    """
    if "dateTime" in marker:
        return datetime.fromisoformat(str(marker["dateTime"])), False

    if "date" in marker:
        return None, True

    return None, False
