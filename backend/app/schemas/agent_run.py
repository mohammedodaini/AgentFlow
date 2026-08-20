"""Agent run and trace shapes (M9).

Layer: schemas — the API boundary.

The interesting decision here is what to *withhold*. `AgentRun` carries
`checkpoint`, which is LangGraph's serialised internal state: a snapshot of
every node's working memory, including the full text of every retrieved chunk.
Returning it would leak the graph's internals into a public contract that could
never be changed afterwards, and would inflate a response by a corpus. It is
absent, exactly as `storage_uri` is absent from `DocumentRead` (M5).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.models.agent_run import RunStatus
from app.repositories.chunk_repository import MAX_TOP_K
from app.schemas.approval import ApprovalRead
from app.schemas.common import APIModel


class AgentRunCreate(BaseModel):
    """Ask the agent a question.

    The same shape as `AskRequest` (M7), deliberately: `/ask` and the agent
    answer the same question with different machinery, and a client comparing
    them should not have to rewrite its request to do so.
    """

    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=MAX_TOP_K)


class SupervisorRequest(BaseModel):
    """One instruction, with no statement of which agent should take it (M15).

    The whole point: before this, a client had to know that a question went to
    `/agent-runs`, a meeting to `/agent-runs/calendar` and a message to
    `/agent-runs/email`. That made the human the router, and the human is worse at
    it than a rule table — they have to learn the product's internal structure
    before they can use it.
    """

    instruction: str = Field(
        min_length=1,
        max_length=2000,
        description="Anything: a question, a meeting to schedule, an email to draft",
    )


class AgentStepRead(APIModel):
    """One step of the trace.

    Returned to clients, not merely kept for operators. The reason is specific
    to AI products: when an answer is wrong, "which passages did it find, and
    did it search twice?" is often a question the *user* can answer faster than
    we can — and an interface that shows its working earns trust that a bare
    answer does not.
    """

    step_index: int
    node_name: str
    tool_name: str | None
    tool_input: dict[str, Any] | None
    tool_output: dict[str, Any] | None
    latency_ms: int
    tokens: int


class AgentRunSummary(APIModel):
    """A run without its trace, for listings.

    A separate model rather than `AgentRunRead` with `steps` omitted, because
    the two are genuinely different resources: a listing of twenty runs each
    dragging its full trace would transfer megabytes to render a table showing
    none of it.
    """

    id: uuid.UUID
    agent_name: str
    status: RunStatus
    error: str | None
    total_tokens: int
    cost_usd: Decimal
    duration_ms: int | None = Field(description="Null while the run is still going")
    created_at: datetime


class AgentRunRead(AgentRunSummary):
    """One run, its answer, and the full trace."""

    input: dict[str, Any] = Field(description="Exactly what the agent was asked")
    output: dict[str, Any] | None = Field(
        default=None, description="The answer and its citations; null until the run finishes"
    )
    steps: list[AgentStepRead]


class SupervisorRead(APIModel):
    """What a supervised instruction produced.

    Two runs, deliberately. `run` is the supervisor's own — it holds the routing
    decision and its reason, and nothing else. `delegated` is the specialist's,
    and it is a first-class run findable in `/agent-runs` under its own agent
    name rather than a step hidden inside this one.

    `delegated` is null exactly when the supervisor refused, which is a
    *successful* outcome with nothing downstream. `reason` says why in words a
    user can act on.
    """

    run: AgentRunRead
    delegated: AgentRunRead | None = None
    approval: ApprovalRead | None = Field(
        default=None,
        description="The row a human must decide on, if the specialist proposed a side effect",
    )
    reason: str = Field(description="Why the work went where it went")
