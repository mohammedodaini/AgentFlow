"""/approvals — proposing side effects, and deciding on them (M12).

Layer: api. Routes call `ApprovalService`, never a graph and never a repository.

Why proposing lives here rather than under /agent-runs
------------------------------------------------------
`POST /agent-runs` (M9) answers a question. This starts a workflow whose *point* is
that a human interrupts it, and grouping it with the approvals it produces keeps the
whole loop — propose, list, decide — readable in one file. A client integrating this
feature reads one module.

Why deciding is POST and not PATCH
----------------------------------
`PATCH /approvals/{id}` with `{"status": "approved"}` is the RESTful-looking shape
and it is wrong here. Approving is not editing a field; it *executes a side effect
on somebody's calendar*. A verb makes that visible at the call site, and it stops a
generic "update this resource" client from authorising a real-world action by
setting a property.

The 409 is the interesting status
---------------------------------
Deciding twice returns 409, not 200. The decision arrives from a browser, browsers
retry, and a double-clicked approve must create one event rather than two — so the
status transition is the idempotency key, and a client that lost the race is told
the decision already happened rather than being quietly given a second one.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentMembership
from app.core.config import Settings, get_settings
from app.db.deps import get_db
from app.integrations import OAuthRegistry, get_oauth_registry
from app.llm import get_llm
from app.llm.base import LLMProvider
from app.rag.embeddings import EmbeddingProvider, get_embedder
from app.schemas.approval import (
    ApprovalRead,
    CalendarActionRequest,
    EmailActionRequest,
    ProposalRead,
    RejectionRequest,
)
from app.services.approval_service import ApprovalService

router = APIRouter(tags=["approvals"])

SessionDep = Annotated[AsyncSession, Depends(get_db)]
EmbedderDep = Annotated[EmbeddingProvider, Depends(get_embedder)]
LLMDep = Annotated[LLMProvider, Depends(get_llm)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
RegistryDep = Annotated[OAuthRegistry, Depends(get_oauth_registry)]


def _service(
    session: AsyncSession, embedder: EmbeddingProvider, llm: LLMProvider, settings: Settings
) -> ApprovalService:
    return ApprovalService(session, embedder, llm, settings)


@router.post("/agent-runs/calendar", summary="Propose a calendar action for approval")
async def propose_calendar_action(
    request: CalendarActionRequest,
    membership: CurrentMembership,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> ProposalRead:
    """Run the calendar agent. **This never creates an event.**

    It returns a paused run and the approval it is waiting on, or — when the
    instruction could not be understood — a finished run and a message saying so.
    Nothing on this path can reach Google: the executor is built only when somebody
    approves.
    """
    run, approval = await _service(session, embedder, llm, settings).propose_calendar_action(
        membership.organization_id, membership.user_id, request.instruction
    )

    if approval is None:
        return ProposalRead(
            agent_run_id=run.id,
            status=run.status.value,
            message=str((run.output or {}).get("refusal", "")),
        )

    return ProposalRead(
        agent_run_id=run.id,
        status=run.status.value,
        approval=ApprovalRead.model_validate(approval),
    )


@router.post("/agent-runs/email", summary="Propose an email for approval")
async def propose_email_action(
    request: EmailActionRequest,
    membership: CurrentMembership,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> ProposalRead:
    """Run the email agent. **This never sends anything.**

    The route M12 said it was deferring, and it is deliberately the same seven
    lines as the calendar one — the safety property comes from the executor being
    built only on the resume path, not from this endpoint being careful.

    The returned `approval.requested_action` contains the **whole message**,
    including the body. That is what the person deciding has to read: a summary of
    an email is a second account of it, and the thing that gets sent is the body.
    """
    run, approval = await _service(session, embedder, llm, settings).propose_email_action(
        membership.organization_id, membership.user_id, request.instruction
    )

    if approval is None:
        return ProposalRead(
            agent_run_id=run.id,
            status=run.status.value,
            message=str((run.output or {}).get("refusal", "")),
        )

    return ProposalRead(
        agent_run_id=run.id,
        status=run.status.value,
        approval=ApprovalRead.model_validate(approval),
    )


@router.get("/approvals", summary="What is waiting for a human")
async def list_approvals(
    membership: CurrentMembership,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[ApprovalRead]:
    """The inbox: pending, unexpired, oldest first.

    A bare list rather than a `Page`, like the transcript endpoint (M10) and for a
    related reason: this is a work queue, not a result set. It is meant to be
    emptied, and offering `total`/`offset` would invite clients to page through
    something whose correct length is zero.
    """
    approvals = await _service(session, embedder, llm, settings).list_pending(
        membership.organization_id, limit=limit
    )
    return [ApprovalRead.model_validate(approval) for approval in approvals]


@router.get("/approvals/{approval_id}", summary="Read one request")
async def get_approval(
    approval_id: uuid.UUID,
    membership: CurrentMembership,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> ApprovalRead:
    """404 for another tenant's request — a distinct status would confirm that a
    specific approval id exists in *some* organization."""
    approval = await _service(session, embedder, llm, settings).get(
        membership.organization_id, approval_id
    )
    return ApprovalRead.model_validate(approval)


@router.post("/approvals/{approval_id}/approve", summary="Permit the action, and do it")
async def approve(
    approval_id: uuid.UUID,
    membership: CurrentMembership,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    settings: SettingsDep,
    registry: RegistryDep,
) -> ApprovalRead:
    """Record the decision, then resume the paused run and execute.

    This is the only route in the application that causes a change outside it. It
    reaches the calendar through a run that was paused, a checkpoint that was
    persisted, and a row a human just decided on — three things that all have to be
    in the right state, none of which a request can supply for itself.
    """
    approval = await _service(session, embedder, llm, settings).approve(
        membership.organization_id, approval_id, membership.user_id, registry
    )
    return ApprovalRead.model_validate(approval)


@router.post("/approvals/{approval_id}/reject", summary="Refuse the action")
async def reject(
    approval_id: uuid.UUID,
    request: RejectionRequest,
    membership: CurrentMembership,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> ApprovalRead:
    """Refuse, and cancel the run that was waiting.

    Needs no OAuth registry, and that asymmetry with `approve` is worth noticing:
    rejecting touches nothing outside this database, so it cannot fail because Google
    is down. The safe decision is also the one with the fewest dependencies.
    """
    approval = await _service(session, embedder, llm, settings).reject(
        membership.organization_id, approval_id, membership.user_id, request.reason
    )
    return ApprovalRead.model_validate(approval)
