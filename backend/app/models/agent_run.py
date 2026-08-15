"""`agent_runs` — one row per top-level agent invocation.

Layer: models. The aggregate root of execution, and deliberately both the unit
of *observability* and the unit of *billing*. Those two are the same row because
they answer the same question from different directions — "what did this run
do?" and "what did this run cost?" — and a system where they live in separate
tables is one where the numbers disagree within a month.

`checkpoint` holds LangGraph's serialised state. That is what lets a run survive
a process restart and, from M12, an approval pause of arbitrary length: the
graph stops, the row persists, and a later request resumes from where it paused
rather than starting again.

`conversation_id` was deliberately absent at M9 — `conversations` did not exist,
and a foreign key to a missing table is not a design decision, it is a broken
migration. M10 built the table and added the column, which is what that comment
was waiting for.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent_step import AgentStep


class RunStatus(enum.StrEnum):
    """Where a run got to.

    `PAUSED_FOR_APPROVAL` exists from the first migration even though M12 is
    what produces it. Adding an enum value later means an `ALTER TYPE`, and a
    status the application can write but the database rejects is a spectacular
    production failure to trade for a one-line saving now.
    """

    RUNNING = "running"
    PAUSED_FOR_APPROVAL = "paused_for_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Whether anything further will happen to this run.

        The question a poller asks, and the one a sweeper asks when deciding
        whether a `running` row from four hours ago is stuck.
        """
        return self in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}


class AgentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One invocation of one agent, from request to terminal state."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        # Every listing is "this organization's runs, newest first". Without the
        # composite index the planner filters by tenant and then sorts the whole
        # result — fine at a thousand rows, not at ten million.
        Index("ix_agent_runs_organization_id_created_at", "organization_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    """The tenant. Every query in `AgentRunRepository` filters on it, and
    deleting an organization takes its runs — a trace nobody may read is not
    worth the storage."""

    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    """Which thread this run belonged to, or NULL for a one-shot invocation
    (`POST /agent-runs`, the eval harness, a future cron job).

    Nullable because a run is not owned by a conversation — it is *attributed*
    to one. `SET NULL` for the same reason `messages.agent_run_id` is: deleting
    a thread must not delete the billing record of work already done and paid
    for. The two tables reference each other in opposite directions, and neither
    reference is allowed to destroy the other's rows.
    """

    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    """Who asked. `SET NULL` rather than `CASCADE`: when an employee leaves, the
    record of what the agent did on their behalf must survive them. That is the
    difference between an audit trail and a convenience."""

    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    """Which graph ran — `rag` today, more from M15.

    A string rather than an enum, deliberately. Agents are code that ships, not
    a closed domain vocabulary, and adding one should not require a migration.
    The trade is that a typo becomes a run nobody can find, which is what the
    `AGENT_NAMES` registry in `app/agents/__init__.py` exists to prevent.
    """

    status: Mapped[RunStatus] = mapped_column(
        Enum(
            RunStatus,
            name="run_status",
            native_enum=True,
            # Store "running", not "RUNNING". M9 omitted this and nothing broke,
            # because SQLAlchemy writes and reads by the same rule either way —
            # so `run_status` became the only enum in the schema holding member
            # *names* while `membership_role`, `document_status`,
            # `document_source` and `task_status` all hold values.
            #
            # The cost of that is invisible until someone types SQL. `WHERE
            # status = 'running'` returns zero rows, with no error, and
            # `docs/database.md` documents the lowercase form. M10 renamed the
            # labels in place (`ALTER TYPE ... RENAME VALUE`) and added this.
            values_callable=lambda enum_class: [member.value for member in enum_class],
        ),
        nullable=False,
    )

    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """What the agent was asked, verbatim.

    Stored so a run can be *replayed*. The most common question about a bad
    answer is "what exactly did we send?", and reconstructing that from logs is
    guesswork the moment a prompt changes.
    """

    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """The answer and its citations. Null until the run reaches a terminal
    state — which is what distinguishes "still working" from "finished with
    nothing to say"."""

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Why it failed, written for the person who asked rather than the person
    who deployed it. The same rule as `documents.error` (M5)."""

    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """LangGraph's serialised state.

    On the run row rather than in a separate checkpoint table, because at one
    checkpoint per run a join costs more than it saves. When M12's approvals
    make runs genuinely long-lived and multi-checkpointed, this is the column to
    revisit — and a migration then is cheaper than a table nobody needs now.
    """

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Separate from `created_at`/`updated_at`, which record when the *row*
    changed. A run paused four days for approval has an `updated_at` that says
    nothing about how long the work took.

    `timezone=True` is spelled out because SQLAlchemy's default for a bare
    `Mapped[datetime]` is `TIMESTAMP WITHOUT TIME ZONE`. The first autogenerated
    migration for this table proved it: `created_at` came out `timezone=True`
    from the mixin while these two did not, in the same `CREATE TABLE`. Naive
    columns holding UTC work perfectly until something compares them to an aware
    value, and then raise `can't subtract offset-naive and offset-aware
    datetimes` — at which point `duration_ms` fails on every run.
    """

    total_tokens: Mapped[int] = mapped_column(nullable=False, default=0)

    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, default=Decimal(0))
    """`Numeric`, never `float`. Money in binary floating point accumulates
    error that surfaces as a bill nobody can reconcile; six decimal places
    because a single cheap call costs fractions of a cent, and rounding each one
    to a cent would discard the entire signal."""

    steps: Mapped[list[AgentStep]] = relationship(
        back_populates="agent_run",
        cascade="all, delete-orphan",
        order_by="AgentStep.step_index",
    )
    """Ordered by `step_index`, because a trace read out of order is not a
    trace. `delete-orphan` so steps cannot outlive the run they explain."""

    @property
    def duration_ms(self) -> int | None:
        """How long the work took, or None while it is still running."""
        if self.finished_at is None:
            return None

        return int((self.finished_at - self.started_at).total_seconds() * 1000)
