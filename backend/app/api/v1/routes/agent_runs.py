"""/agent-runs — invoke the agent, and read what it did (M9).

Layer: api. Routes call `AgentService`, never a graph. That boundary is what
guarantees every execution has a run row and a trace: a route invoking
`build_graph` directly would produce an answer with no record, and the first
question asked about it would be unanswerable.

Why 200 and not 202
-------------------
Upload (M5) answers 202 because ingestion is genuinely background work. This
runs inline and answers 200 with the finished result, because an agent run takes
seconds rather than minutes, and a poll loop for a two-second wait is friction
with no benefit.

That will not hold. M12's approvals make a run pause for as long as a human
takes to click, at which point this endpoint returns a `paused_for_approval` run
and the client polls `GET /agent-runs/{id}` — which is exactly why that endpoint
exists now, before anything needs to poll it. `RunStatus` already carries the
value; the response shape will not have to change.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentMembership
from app.core.config import Settings, get_settings
from app.db.deps import get_db
from app.llm import get_llm
from app.llm.base import LLMProvider
from app.models.agent_run import AgentRun, RunStatus
from app.rag.embeddings import EmbeddingProvider, get_embedder
from app.schemas.agent_run import (
    AgentRunCreate,
    AgentRunRead,
    AgentRunSummary,
    AgentStepRead,
    SupervisorRead,
    SupervisorRequest,
)
from app.schemas.approval import ApprovalRead
from app.schemas.common import Page
from app.services.agent_service import AgentService
from app.services.supervisor_service import SupervisorService

router = APIRouter(prefix="/agent-runs", tags=["agents"])

SessionDep = Annotated[AsyncSession, Depends(get_db)]
EmbedderDep = Annotated[EmbeddingProvider, Depends(get_embedder)]
LLMDep = Annotated[LLMProvider, Depends(get_llm)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _service(
    session: AsyncSession, embedder: EmbeddingProvider, llm: LLMProvider, settings: Settings
) -> AgentService:
    return AgentService(session, embedder, llm, settings)


@router.post("", summary="Run the RAG agent on a question")
async def create_run(
    request: AgentRunCreate,
    membership: CurrentMembership,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> AgentRunRead:
    """Execute the agent and return the finished run with its trace.

    `CurrentMembership`, so the corpus searched belongs to one tenant and the
    caller has proved membership of it. That id is what `build_search_chunks`
    closes over — the model never receives it and cannot influence it, which is
    the property that stops prompt injection reaching another customer's
    documents.
    """
    run = await _service(session, embedder, llm, settings).run_rag_agent(
        membership.organization_id,
        request.question,
        user_id=membership.user_id,
        top_k=request.top_k,
    )

    return _read(run)


@router.post("/supervised", summary="Run whichever agent fits the instruction")
async def create_supervised_run(
    request: SupervisorRequest,
    membership: CurrentMembership,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> SupervisorRead:
    """**The single entry point (M15).** Say what you want; it decides who acts.

    Every other agent endpoint in this API requires the caller to already know
    which agent they need. That was the right shape while there was one agent, and
    it made the *human* the router the moment there were three — they had to learn
    the product's internal structure before they could use it.

    Nothing here is a new path to a side effect. The supervisor classifies and
    delegates to the same methods these other routes call, so a calendar or email
    action still reaches a provider only through an `approvals` row a human
    decided on. Adding an entry point must not add a capability, and this one does
    not.

    A refusal is a 200 with `delegated: null` and a `reason` naming what this
    product *can* do. It is not a 4xx: the request was well-formed and understood,
    and the answer is that nothing here serves it.
    """
    outcome = await SupervisorService(session, embedder, llm, settings).run(
        membership.organization_id,
        request.instruction,
        user_id=membership.user_id,
    )

    return SupervisorRead(
        run=_read(outcome.run),
        delegated=_read(outcome.delegated) if outcome.delegated else None,
        # The approval row itself, not just the action it permits. Returning the
        # action alone was the first shape of this response, and it hid the bug
        # M15 found at runtime: a paused run with no row behind it looked
        # identical to a working one.
        approval=ApprovalRead.model_validate(outcome.approval) if outcome.approval else None,
        reason=outcome.reason,
    )


@router.get("", summary="List this organization's agent runs")
async def list_runs(
    membership: CurrentMembership,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    settings: SettingsDep,
    status: Annotated[RunStatus | None, Query(description="Filter by status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AgentRunSummary]:
    """Summaries only — traces are fetched one run at a time."""
    runs, total = await _service(session, embedder, llm, settings).list_runs(
        membership.organization_id, status=status, limit=limit, offset=offset
    )

    return Page[AgentRunSummary](
        items=[AgentRunSummary.model_validate(run) for run in runs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{run_id}", summary="Read one run and its full trace")
async def get_run(
    run_id: uuid.UUID,
    membership: CurrentMembership,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> AgentRunRead:
    """404 for another tenant's run, not 403 — a distinct status would turn this
    into an oracle for enumerating run ids across organizations."""
    run = await _service(session, embedder, llm, settings).get_run(
        membership.organization_id, run_id
    )

    return _read(run)


def _read(run: AgentRun) -> AgentRunRead:
    """Build the response from the ORM object.

    Written out rather than relying on `from_attributes` alone, because `steps`
    has to be mapped explicitly — an implicit conversion would work today and
    silently start publishing any column later added to `AgentStep`, which is
    the failure `DocumentRead` avoids by whitelisting.
    """
    return AgentRunRead(
        id=run.id,
        agent_name=run.agent_name,
        status=run.status,
        error=run.error,
        total_tokens=run.total_tokens,
        cost_usd=run.cost_usd,
        duration_ms=run.duration_ms,
        created_at=run.created_at,
        input=run.input,
        output=run.output,
        steps=[
            AgentStepRead(
                step_index=step.step_index,
                node_name=step.node_name,
                tool_name=step.tool_name,
                tool_input=step.tool_input,
                tool_output=step.tool_output,
                latency_ms=step.latency_ms,
                tokens=step.tokens,
            )
            for step in run.steps
        ],
    )
