"""Calendar tools — the first tools in this codebase with a side effect.

Layer: agents. Every tool before this one *read*: `search_chunks` (M9) returns
passages, recall returns memories. Nothing an agent did could be seen from outside
the system. Putting an event on somebody's calendar can.

The shape that makes that safe
------------------------------
**Proposing and executing are two different functions, and only one of them is a
tool the graph can reach freely.**

`parse_event_request` turns an instruction into a *description* of an action. It
touches nothing. `build_create_event` returns the thing that actually calls
Google, and the graph can only get to it on the resume path — after an `approvals`
row exists and a human has decided on it.

The alternative — one `create_event` tool with an `if approved:` inside — puts the
decision and the effect in the same function, where a refactor, an early return or
a second caller can separate them. Here the effect is unreachable without having
gone through the row.

**The action dict is the contract.** It is what gets stored in
`approvals.requested_action`, shown to the human, and executed on approval — the
same object all three times, so "what was approved" and "what ran" cannot drift
apart. That is why it is a plain JSON-serialisable dict rather than an object: it
has to survive a round trip through JSONB and a process restart.

Deterministic parsing, and the honest reason
--------------------------------------------
`parse_event_request` reads times out of an instruction with a regular expression.
A model would do this far better — "next Tuesday afternoon" is exactly the kind of
judgement `docs/agents.md` says LLM calls are *for*.

It is deterministic here for the reason every other model-shaped decision in this
project is: `LLMProvider` is a text-in/text-out seam with no key behind it, and a
parser that quietly produced a plausible-but-wrong datetime would put a meeting in
somebody's diary at the wrong time. When the parse fails this refuses rather than
guessing — and the graph proposes nothing, which is the safe direction.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import structlog
from langchain_core.tools import BaseTool, StructuredTool
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.integrations import OAuthRegistry
from app.integrations.google_calendar.client import GoogleCalendarClient
from app.models.integration import Provider
from app.services.integration_service import IntegrationService

logger = structlog.get_logger(__name__)

CREATE_EVENT = "create_calendar_event"
LIST_EVENTS = "list_calendar_events"

PROPOSED_ACTION_KIND = "calendar.create_event"
"""The `kind` discriminator on a stored action.

Present from the first action, with one value, so that the day a second kind
exists (an email draft, M14) nothing has to guess what an untagged blob was. A
`requested_action` nobody can classify is a row nobody can execute or explain.
"""

DEFAULT_DURATION = timedelta(hours=1)
"""How long a proposed meeting lasts when the instruction does not say. An hour,
because the alternative — refusing to propose anything without an explicit end —
makes the common case unusable, and the human approving it can see the end time."""

MAX_SUMMARY_LENGTH = 200

_WHEN = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})[ T](?P<time>\d{2}:\d{2})")
"""An explicit `YYYY-MM-DD HH:MM`, and nothing looser.

Deliberately unable to parse "tomorrow at 3". Half-understanding a date is worse
than not understanding it: "3" could be either end of the day, and a wrong guess
becomes a meeting somebody misses rather than an error somebody reads.
"""


def parse_event_request(instruction: str) -> dict[str, Any] | None:
    """Turn an instruction into a proposed action, or None if it cannot.

    Returns a plain dict because this is what gets written to JSONB, shown to a
    human, and executed later — see the module docstring. Returning None is a
    first-class outcome: the graph proposes nothing and the run ends having done no
    harm.
    """
    match = _WHEN.search(instruction)

    if match is None:
        return None

    try:
        starts_at = datetime.fromisoformat(f"{match.group('date')}T{match.group('time')}:00+00:00")
    except ValueError:
        # A syntactically well-formed but impossible date — 2026-02-30. The regex
        # cannot know that; `fromisoformat` does.
        return None

    return {
        "kind": PROPOSED_ACTION_KIND,
        "summary": _summarise(instruction[: match.start()].strip() or instruction),
        # Serialised on the way in rather than on the way out, so what the human is
        # shown and what the executor reads are byte-identical. A dict that is
        # "nearly JSON" is one that changes shape when it crosses JSONB.
        "starts_at": starts_at.isoformat(),
        "ends_at": (starts_at + DEFAULT_DURATION).isoformat(),
    }


def describe(action: dict[str, Any]) -> str:
    """The one line a human reads before deciding.

    Rendered from the action by code, never by a model. Somebody is authorising a
    side effect, and the sentence in front of them has to be a faithful rendering
    of the thing that will execute — not a second, prettier description that might
    not match it.
    """
    starts_at = datetime.fromisoformat(action["starts_at"])
    return f"Create a calendar event '{action['summary']}' on {starts_at:%A %d %B %Y at %H:%M} UTC"


def build_create_event(
    session: AsyncSession,
    registry: OAuthRegistry,
    settings: Settings,
    organization_id: uuid.UUID,
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """The executor. Reachable only on the resume path, after an approval.

    The tenant is closed over and never appears in the action — ADR-0012's rule,
    and it matters more here than it did for search. A tool argument is
    model-chosen, so an `organization_id` parameter would let a prompt-injected
    instruction write an event into another customer's calendar. Reading is a leak;
    writing is a leak that leaves a trace in somebody else's diary.

    Deliberately *not* a `BaseTool`. Tools are things a graph may call; this is
    something only the resume path may call, and giving it the same type as
    `search_chunks` would invite exactly the mistake this module is shaped to
    prevent.
    """

    async def execute(action: dict[str, Any]) -> dict[str, Any]:
        integrations = IntegrationService(session, _no_redis(), registry, settings)
        _, access_token = await integrations.get_fresh_token(
            organization_id, Provider.GOOGLE_CALENDAR
        )

        event = await GoogleCalendarClient().create_event(
            access_token,
            summary=action["summary"],
            starts_at=datetime.fromisoformat(action["starts_at"]),
            ends_at=datetime.fromisoformat(action["ends_at"]),
        )

        logger.info(
            "calendar.event_created",
            organization_id=str(organization_id),
            event_id=event.event_id,
        )
        return {"event_id": event.event_id, "url": event.url, "title": event.title}

    return execute


def build_list_events(
    session: AsyncSession,
    registry: OAuthRegistry,
    settings: Settings,
    organization_id: uuid.UUID,
) -> BaseTool:
    """Reading the calendar — a real tool, because reading needs no permission.

    The asymmetry is the milestone in one file: this is a `BaseTool` the graph may
    call whenever it likes, and `build_create_event` above is not.
    """

    async def list_calendar_events(limit: int = 10) -> list[dict[str, Any]]:
        integrations = IntegrationService(session, _no_redis(), registry, settings)
        _, access_token = await integrations.get_fresh_token(
            organization_id, Provider.GOOGLE_CALENDAR
        )

        events = await GoogleCalendarClient().list_events(access_token, limit=limit)
        return [
            {
                "event_id": event.event_id,
                "title": event.title,
                "starts_at": event.starts_at.isoformat() if event.starts_at else None,
                "all_day": event.all_day,
            }
            for event in events
        ]

    return StructuredTool.from_function(
        coroutine=list_calendar_events,
        name=LIST_EVENTS,
        description="List upcoming events on the organization's connected calendar.",
    )


def _summarise(text: str) -> str:
    """A title for the event, from the instruction, bounded to the column."""
    cleaned = " ".join(text.split())

    for prefix in ("Schedule ", "schedule ", "Book ", "book "):
        cleaned = cleaned.removeprefix(prefix)

    cleaned = cleaned.rstrip(" ,;:").removesuffix(" on").removesuffix(" at")

    return cleaned[:MAX_SUMMARY_LENGTH] if cleaned else "Meeting"


def _no_redis() -> Any:
    """`IntegrationService` needs a Redis handle only for the OAuth *connect* flow,
    which nothing here performs — these calls reach `get_fresh_token` and nothing
    else.

    Passing None rather than threading a real client through every calendar caller
    keeps the dependency honest about where it is actually used. If a future path
    reaches the state store from here it fails loudly on None rather than quietly
    sharing a connection nobody meant to give it.
    """
    return None
