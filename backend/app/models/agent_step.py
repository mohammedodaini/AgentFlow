"""`agent_steps` — the trace: every node and tool call inside a run.

Layer: models. This table answers "why did the agent do that?", and it is the
reason M9 builds tracing alongside the first agent rather than after it.

An agent without a trace is not debuggable, it is *anecdotal*. A user reports a
wrong answer; without these rows the only available response is to run the same
question again and hope it misbehaves identically — which, with a non-zero
temperature or a changed corpus, it will not. With them, the exact retrieval,
the exact context and the exact model reply are on disk.

No `organization_id` here. Scope comes from a join to `agent_runs`, for the same
reason `document_chunks` has none (M6): denormalising the tenant onto the child
row creates a way for the two to disagree, and a step whose tenant no longer
matches its run is unreachable at best and cross-tenant at worst.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent_run import AgentRun


class AgentStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One node execution within a run."""

    __tablename__ = "agent_steps"
    __table_args__ = (
        # A run's steps are read as an ordered sequence, always. The unique
        # constraint makes that ordering *meaningful* — two rows claiming step 3
        # is a trace nobody can reconstruct — and it serves the lookup too, so
        # no separate index on `agent_run_id` is needed.
        UniqueConstraint("agent_run_id", "step_index"),
        # "Which runs used the retriever, and how slow was it?" is among the
        # first questions anyone asks of a trace table, and it filters here.
        Index("ix_agent_steps_tool_name", "tool_name"),
    )

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    """A step never exists without its run. `CASCADE` in the database rather
    than a Python loop that can be interrupted halfway."""

    step_index: Mapped[int] = mapped_column(nullable=False)
    """Position in the run, from 0. Assigned by the recorder rather than derived
    from `created_at`: two steps in the same millisecond are ordinary, and a
    trace ordered by timestamp would shuffle them."""

    node_name: Mapped[str] = mapped_column(String(64), nullable=False)
    """Which graph node ran — `retrieve`, `generate`. The structural half of the
    trace: it says what the graph *did*, which is how a conditional edge taken
    wrongly becomes visible."""

    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """Which tool the node called, if any. Null for a node that only transformed
    state, which is a real and common case and not worth a second table."""

    tool_input: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    tool_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """What went in, and what came back.

    JSONB rather than text so a trace can be *queried* — "every run where
    retrieval returned nothing" is a `WHERE` clause, not a log grep. The cost is
    that anything stored here must be JSON-serialisable, which is a genuine
    constraint on what a node may record, and it is enforced at the call site.
    """

    latency_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    """Per step, not only per run. A run that took nine seconds is a fact; a run
    that took nine seconds *of which eight were one retrieval* is a diagnosis."""

    tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    """Attributed to the step that spent them, so a run's total can be explained
    rather than merely reported. This is the column M12's cost accounting
    aggregates."""

    agent_run: Mapped[AgentRun] = relationship(back_populates="steps")
