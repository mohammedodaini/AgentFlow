"""Conversation + message API shapes (M10).

Layer: schemas — the API boundary.

The whitelist matters more here than almost anywhere else. `Conversation` and
`Message` are the tables closest to a person: what they typed, when, and which
model run answered them. Returning ORM objects would publish every column added
later by default, and the columns most likely to be added to a chat table are
exactly the ones nobody meant to expose.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.conversation import TITLE_MAX_LENGTH
from app.models.message import MAX_MESSAGE_CHARS, MessageRole
from app.schemas.common import APIModel


class ConversationCreate(BaseModel):
    """Open a thread. The title is optional and usually omitted.

    Clients rarely have a title before the first message, which is why one is
    derived from it — see `ConversationService._clip`.
    """

    title: str | None = Field(default=None, max_length=TITLE_MAX_LENGTH)


class ConversationRead(APIModel):
    """One thread, without its messages."""

    id: uuid.UUID
    title: str
    archived_at: datetime | None = Field(default=None, description="Null while the thread is live")
    created_at: datetime


class MessageCreate(BaseModel):
    """One turn from the person.

    `max_length` is enforced here rather than by the column, which is `Text` and
    has no length in Postgres. Without it, one pasted log file becomes a message
    nobody can render and a prompt nobody can afford — and the failure would
    surface as a model error rather than a 422.
    """

    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class MessageRead(APIModel):
    """One turn in a transcript.

    `agent_run_id` is published deliberately: it is what lets a client link the
    sentence it is rendering to `GET /agent-runs/{id}` and show its working. The
    trace is already a client-facing surface (ADR-0012), and a chat reply with no
    route back to the run that produced it is the one place that argument would
    break down.
    """

    id: uuid.UUID
    role: MessageRole
    content: str
    agent_run_id: uuid.UUID | None
    token_usage: dict[str, Any] | None
    created_at: datetime


class TurnRead(BaseModel):
    """What `POST /conversations/{id}/messages` answers with.

    Both messages, not only the reply. The client has just sent text and needs
    the persisted `id` and `created_at` of its own turn to render it without
    guessing — returning just the answer would force an immediate re-fetch of the
    transcript to get them.
    """

    conversation: ConversationRead
    user_message: MessageRead
    assistant_message: MessageRead
