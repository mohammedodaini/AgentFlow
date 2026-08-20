"""Approval API shapes (M12).

Layer: schemas — the API boundary.

`requested_action` is published in full, which is the opposite of the decision
`AgentRunRead` makes about `checkpoint`. Both are right, and the difference is the
point: a checkpoint is the graph's internal working memory and nobody outside needs
it, while the requested action is *the thing being authorised*. A person cannot
meaningfully approve something they are not shown.

`summary` is published alongside it rather than instead of it. The summary is what
a UI renders; the action is what a careful user — or an auditor — expands to check
that the sentence matches the payload. Publishing only the summary would make the
prettier of the two the only one anybody could see.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.approval import ApprovalStatus
from app.schemas.common import APIModel


class CalendarActionRequest(BaseModel):
    """Ask the calendar agent to propose something.

    Deliberately not called `CreateEventRequest`: this endpoint never creates an
    event. It produces a *proposal*, and the naming should not invite a client
    author to assume otherwise.
    """

    instruction: str = Field(
        min_length=1,
        max_length=2000,
        description="What to schedule, including an explicit date and time",
    )


class EmailActionRequest(BaseModel):
    """What to ask the email agent for.

    Named for the *proposal*, not the send, for the same reason
    `CalendarActionRequest` is: this endpoint cannot send an email, and a schema
    called `SendEmailRequest` would describe an operation the route does not
    perform.
    """

    instruction: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "What to write, in the form: email alice@example.com about the Q3 "
            "numbers saying the report is ready."
        ),
    )
    """Longer than the calendar's limit, because this one carries a message body
    rather than a date. Still bounded: it becomes a JSONB action and, eventually,
    a prompt."""


class ApprovalRead(APIModel):
    """One request awaiting — or having received — a human decision."""

    id: uuid.UUID
    agent_run_id: uuid.UUID = Field(description="The run paused on this decision")
    status: ApprovalStatus
    summary: str = Field(description="One line describing what will happen, rendered by code")
    requested_action: dict[str, Any] = Field(
        description="The literal action that will execute, unchanged, on approval"
    )
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    expires_at: datetime
    reason: str | None = Field(default=None, description="Why it was rejected, if it was")
    created_at: datetime


class ProposalRead(BaseModel):
    """What proposing returns: the run, and the approval it is waiting on.

    `approval` is nullable because a run that understood nothing produces no
    request. A client seeing `approval: null` should show the run's refusal rather
    than an empty inbox item — which is why `message` carries it directly instead of
    forcing a second fetch.
    """

    agent_run_id: uuid.UUID
    status: str = Field(description="The run's status — paused_for_approval, or succeeded")
    approval: ApprovalRead | None = None
    message: str | None = Field(
        default=None, description="Why nothing was proposed, when nothing was"
    )


class RejectionRequest(BaseModel):
    """Why the human said no.

    Optional, because requiring a reason makes rejecting slower than approving — and
    a UI that makes the safe choice the tedious one gets fewer safe choices.
    """

    reason: str | None = Field(default=None, max_length=1000)
