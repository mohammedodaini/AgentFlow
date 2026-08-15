"""Pricing arithmetic, the calendar parser, and approval expiry (M12).

Everything here runs without a database, a model or a network. Three separate
things, and each fails in a way nobody would notice at runtime:

- **Money computed in `float`** looks right for years and then produces a bill
  nobody can reconcile.
- **A parser that half-understands a date** puts a meeting in somebody's diary at
  the wrong time. It does not raise; a person misses a meeting.
- **An approval whose expiry is only advisory** lets an action composed last month
  execute against this month's facts.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.agents.calendar.tools import (
    DEFAULT_DURATION,
    MAX_SUMMARY_LENGTH,
    PROPOSED_ACTION_KIND,
    describe,
    parse_event_request,
)
from app.llm.pricing import cost_of
from app.models.approval import Approval, ApprovalStatus

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def approval(**overrides: Any) -> Approval:
    """An in-memory approval. Never flushed — these tests are about pure logic."""
    defaults: dict[str, Any] = {
        "agent_run_id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "requested_action": {"kind": PROPOSED_ACTION_KIND},
        "summary": "Create a calendar event",
        "status": ApprovalStatus.PENDING,
        "expires_at": NOW + timedelta(hours=1),
    }
    return Approval(**(defaults | overrides))


# --------------------------------------------------------------------------
# pricing
# --------------------------------------------------------------------------


def test_no_configured_rates_means_no_claimed_cost() -> None:
    """The default, and it means "nobody has told this system what it pays" — not
    "this run was free".

    M9 stored `Decimal(0)` rather than guess a rate. M12 owns pricing and keeps that
    refusal: the arithmetic ships, the numbers are the operator's.
    """
    cost = cost_of(
        input_tokens=10_000, output_tokens=2_000, input_rate=Decimal(0), output_rate=Decimal(0)
    )

    assert cost == Decimal("0.000000")


def test_input_and_output_are_priced_separately() -> None:
    """Every provider prices them separately, usually with output several times
    dearer. A blended rate would be wrong for every workload whose shape differs from
    whatever mix the blend was computed against."""
    cost = cost_of(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        input_rate=Decimal(3),
        output_rate=Decimal(15),
    )

    assert cost == Decimal("18.000000")


def test_the_result_is_a_decimal_all_the_way_down() -> None:
    """`Numeric(10, 6)` was chosen so money never touches binary floating point. One
    `float` anywhere in the chain reintroduces exactly the error the column type
    exists to avoid."""
    cost = cost_of(
        input_tokens=333, output_tokens=333, input_rate=Decimal(3), output_rate=Decimal(15)
    )

    assert isinstance(cost, Decimal)
    assert cost.as_tuple().exponent == -6


def test_a_tiny_run_still_records_something() -> None:
    """Six decimal places exist because a single cheap call costs fractions of a
    cent. Rounding to two would record every real run as zero."""
    cost = cost_of(
        input_tokens=1_000, output_tokens=100, input_rate=Decimal(3), output_rate=Decimal(15)
    )

    assert cost > Decimal(0)


def test_negative_token_counts_are_rejected() -> None:
    """A caller bug, not a free run. Silently pricing it at zero would hide the
    arithmetic error that produced it."""
    with pytest.raises(ValueError, match="cannot be negative"):
        cost_of(input_tokens=-1, output_tokens=0, input_rate=Decimal(1), output_rate=Decimal(1))


# --------------------------------------------------------------------------
# the parser
# --------------------------------------------------------------------------


def test_an_explicit_date_and_time_is_understood() -> None:
    action = parse_event_request("Schedule a design review on 2026-08-20 09:00")

    assert action is not None
    assert action["kind"] == PROPOSED_ACTION_KIND
    assert action["starts_at"] == "2026-08-20T09:00:00+00:00"
    assert "design review" in action["summary"]


def test_the_end_time_follows_from_the_default_duration() -> None:
    action = parse_event_request("Standup 2026-08-20 09:00")

    assert action is not None
    starts_at = datetime.fromisoformat(action["starts_at"])
    assert datetime.fromisoformat(action["ends_at"]) == starts_at + DEFAULT_DURATION


def test_a_vague_time_is_refused_rather_than_guessed() -> None:
    """**The most important assertion about this parser.**

    "tomorrow at 3" could be either end of the day. Half-understanding it produces a
    meeting at the wrong time — which does not raise, and which somebody discovers by
    missing it. Refusing produces a message telling the user how to succeed.
    """
    assert parse_event_request("Schedule a review tomorrow at 3") is None


def test_an_impossible_date_is_refused() -> None:
    """The regex cannot know February has no thirtieth; `fromisoformat` does."""
    assert parse_event_request("Schedule a review on 2026-02-30 09:00") is None


def test_an_instruction_with_no_time_at_all_is_refused() -> None:
    assert parse_event_request("Please sort out my calendar") is None


def test_the_summary_is_bounded_to_the_column() -> None:
    """The event title is `String(200)`. An unbounded title is an `IntegrityError`
    raised from inside a graph."""
    action = parse_event_request("Schedule " + "x" * 400 + " on 2026-08-20 09:00")

    assert action is not None
    assert len(action["summary"]) <= MAX_SUMMARY_LENGTH


def test_an_action_is_json_serialisable() -> None:
    """It has to survive a round trip through JSONB and a process restart — which is
    what makes resuming after a deploy possible at all."""
    action = parse_event_request("Sync 2026-08-20 09:00")

    assert action is not None
    assert json.loads(json.dumps(action)) == action


# --------------------------------------------------------------------------
# the sentence a human reads
# --------------------------------------------------------------------------


def test_the_summary_describes_the_action_it_will_execute() -> None:
    """Rendered from the action by code, never by a model.

    Somebody is authorising a side effect, and the sentence in front of them has to
    be a faithful rendering of the thing that runs — not a second, prettier account
    that might not match.
    """
    action = parse_event_request("Schedule a design review on 2026-08-20 09:00")

    assert action is not None
    sentence = describe(action)

    assert "design review" in sentence
    assert "20 August 2026" in sentence
    assert "09:00" in sentence


# --------------------------------------------------------------------------
# expiry
# --------------------------------------------------------------------------


def test_a_fresh_approval_is_actionable() -> None:
    assert approval().is_pending
    assert not approval().is_expired(now=NOW)


def test_an_overdue_approval_is_expired_by_the_clock() -> None:
    """Computed on read rather than trusted from `status`, because expiry happens by
    the clock and nothing writes a row when it passes. Until a sweep runs, this is
    what stops an action executing an hour after it should have."""
    stale = approval(expires_at=NOW - timedelta(minutes=1))

    assert stale.status is ApprovalStatus.PENDING
    assert stale.is_expired(now=NOW)


def test_a_decided_approval_is_no_longer_pending() -> None:
    """The transition is the idempotency key — the thing that makes a double-clicked
    approve create one event rather than two."""
    assert not approval(status=ApprovalStatus.APPROVED).is_pending
    assert not approval(status=ApprovalStatus.REJECTED).is_pending
    assert not approval(status=ApprovalStatus.EXPIRED).is_pending
