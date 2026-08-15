"""`approvals` — human-in-the-loop as a database record, not an in-memory flag.

Layer: models. The table that makes an agent safe to give side effects.

Why a row, and not just a LangGraph interrupt
---------------------------------------------
LangGraph can pause a graph mid-run and wait. That pause lives in a process, and
processes die: a deploy, a crash, an autoscaler scaling in, a laptop closing. The
gap between "please approve this" and somebody clicking is measured in *hours* —
lunch, a meeting, a weekend — so a restart in that window is not an edge case, it
is the expected case.

An approval existing only as an interrupt therefore vanishes silently at the first
restart. Nothing errors. The user clicks approve and gets a 404, or nothing happens
at all, and nobody finds out until a customer asks why the email they approved
never went.

So the interrupt is the *mechanism* and this table is the *truth*. The row is
written before the graph pauses and survives everything: the run can be resumed by
a different process, on a different host, a week later.

It is also the audit trail, which is the second reason it is a table. "Who approved
sending that, and when?" is a question a compliance review will ask, and an
in-memory pause has no answer to it.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

DEFAULT_TTL_HOURS = 24
"""How long a pending approval stays actionable.

A day: long enough to survive a request landing on a Friday evening, short enough
that nobody approves an action whose context has gone stale. The action was
composed against facts that were true when it was proposed — a free meeting slot,
an invoice total — and approving it a month later executes it against facts that
may not be.

Expiry is a *safety* property, not tidiness. An approval queue with no expiry
accumulates actions that get approved eventually, by somebody who no longer
remembers why they were proposed.
"""


class ApprovalStatus(enum.StrEnum):
    """Where a request for permission got to."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

    EXPIRED = "expired"
    """Nobody decided in time.

    A *normal* outcome, not an error. A run that ends without acting because nobody
    approved it is the system working correctly — that is what asking permission
    means.
    """


class Approval(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One request for a human to permit one specific action."""

    __tablename__ = "approvals"
    __table_args__ = (
        # "What is waiting for me?" — the only query the inbox makes, and the one a
        # notification job will make on a schedule.
        Index("ix_approvals_organization_id_status", "organization_id", "status"),
        # One approval per run, for now. M12 gates a single action per run, and the
        # constraint says so out loud rather than leaving a second row
        # possible-but-unhandled. A future multi-step plan needs a `step_index`
        # here and a deliberate decision about what partial approval means.
        Index("uq_approvals_agent_run_id", "agent_run_id", unique=True),
    )

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    """The run that is paused waiting for this.

    `CASCADE`, unusually for this schema, and deliberately: an approval without its
    run is unresumable. There is nothing to execute, nothing to show the user for
    context, and no way to explain what they would be permitting.
    """

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    """Denormalised from the run, and worth the duplication.

    Every read here is "this organization's pending approvals", so reaching the
    tenant through `agent_runs` would make the inbox — the most frequent query in
    the feature — a join. `messages` reaches its tenant through a join precisely
    because it is *not* queried that way.
    """

    requested_action: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """The literal action, complete enough to execute without re-deriving it.

    **The most important column in the table.** The tempting design stores a
    reference — a plan id, a tool name — and rebuilds the action when the user
    approves. That means the thing executed is whatever the code produces *now*,
    while the thing approved was whatever was shown *then*. A prompt change, a
    clock tick, or a re-run of a non-deterministic step between the two, and
    somebody has authorised one action and performed another.

    Storing the whole action makes "what was approved" and "what ran" the same
    object by construction.
    """

    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(
            ApprovalStatus,
            name="approval_status",
            native_enum=True,
            values_callable=lambda enum_class: [member.value for member in enum_class],
        ),
        nullable=False,
        default=ApprovalStatus.PENDING,
    )

    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    """Who decided. `SET NULL` rather than `CASCADE`: when somebody leaves, the
    record that *a person* authorised this action must outlive their account. That
    is the difference between an audit trail and a convenience — and here it is the
    whole point of the table."""

    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """When this stops being actionable.

    Stored rather than computed from `created_at + TTL`, so changing the default
    tomorrow does not retroactively expire — or revive — every approval already
    waiting. A decision made under one policy keeps that policy.
    """

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Why it was rejected, in the rejector's words.

    Optional, and only meaningful on rejection. It exists because "no" with no
    reason teaches nobody anything: the agent proposes the same action tomorrow,
    and the same person rejects it again.
    """

    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    """One line describing what will happen, for the person deciding.

    Written by code from the action, never by a model. Somebody is being asked to
    authorise a side effect, and the sentence they read has to be a faithful
    rendering of `requested_action` — not a second, prettier description that a
    model produced and that might not match what executes.
    """

    @property
    def is_pending(self) -> bool:
        return self.status is ApprovalStatus.PENDING

    def is_expired(self, *, now: datetime | None = None) -> bool:
        """Whether the window has closed.

        Computed on read rather than trusted from `status`, because expiry happens
        by the clock and not by anything writing a row. A sweep will eventually
        mark these `EXPIRED`; until it runs, this is what stops an action being
        executed an hour after it should have been.
        """
        return (now or datetime.now(UTC)) >= self.expires_at
