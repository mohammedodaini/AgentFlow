"""`messages` — APPEND-ONLY chat turns.

Layer: models. Editing history destroys auditability, and this table is the
record of what an automated system told a person about their own company. There
is no update path in `ConversationRepository` by design: a turn is written once,
and a correction is a new turn.

`agent_run_id` (nullable) links an assistant message to the run that produced
it — the bridge between chat UX and the execution trace. Nullable because not
every assistant turn comes from a run: a refusal issued before any agent starts
still belongs in the transcript, and a placeholder run for it would be a lie in
the billing table.

The ordering key, and why it is `created_at`
--------------------------------------------
Turns are read oldest-first, and the sort key is `created_at` rather than an
explicit sequence column. That is safe *here* specifically because ids are
UUIDv7 (ADR-0003) and the tie-break falls back to them: two messages written in
the same millisecond — a user turn and its reply cannot be, but a seeded
fixture's can — still order deterministically. Random UUIDs would have made this
a coin toss, and a transcript that shuffles under you is worse than a slow one.
"""

from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Enum, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.conversation import Conversation

MAX_MESSAGE_CHARS = 20_000
"""A ceiling on one turn, enforced at the API boundary rather than by the column.

`Text` has no length in Postgres, which is the right storage decision and the
wrong validation decision: without a bound, one paste of a log file becomes a
message nobody can render and a prompt nobody can afford. 20k characters is
roughly 5k tokens — comfortably more than any real question, and far less than
the whole history budget.
"""


class MessageRole(enum.StrEnum):
    """Who is speaking.

    The three provider-shaped roles, because that is the vocabulary every model
    API and every chat UI already speaks. Inventing our own would mean
    translating at both boundaries.
    """

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    """Unused at M10 and present from the first migration anyway.

    The same argument as `RunStatus.PAUSED_FOR_APPROVAL` (M9): adding a value to
    a native Postgres enum later is an `ALTER TYPE`, and a role the application
    can write but the database rejects is a spectacular way to lose a
    transcript. M15's multi-agent work is what writes it.
    """


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One turn in a conversation. Written once, never updated."""

    __tablename__ = "messages"
    __table_args__ = (
        # Every read is "this thread, in order" — the transcript a user sees and
        # the history a prompt gets are the same query with different limits.
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    """Tenancy is reached through the conversation, exactly as `document_chunks`
    reaches it through `documents` (M6). No `organization_id` column here: a
    denormalised tenant that can disagree with its parent is a tenancy bug
    waiting for someone to write the wrong join."""

    role: Mapped[MessageRole] = mapped_column(
        Enum(
            MessageRole,
            name="message_role",
            native_enum=True,
            # Stores "assistant", not "ASSISTANT" — the same label the API
            # publishes and `docs/database.md` documents. Every enum in this
            # schema does this except `run_status`, which M9 got wrong and M10
            # renamed; see the comment there.
            values_callable=lambda enum_class: [member.value for member in enum_class],
        ),
        nullable=False,
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)

    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    """Which run produced this reply, for assistant turns.

    `SET NULL` rather than `CASCADE`: a retention policy that eventually prunes
    old traces must not take the transcript with it. The user's record of the
    conversation and our record of how we produced it have different lifetimes,
    and this is the column that lets them.
    """

    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """What this turn cost, as `{"input": 812, "output": 96}`.

    Denormalised from the run on purpose. A thread's cost is the sum of its
    turns, and computing that by joining to `agent_runs` breaks the moment a run
    is pruned or a turn has no run at all. Stored per turn, the number survives
    both.
    """

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
