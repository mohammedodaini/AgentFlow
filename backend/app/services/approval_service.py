"""Asking a human, and doing what they said.

Layer: services. Owns the `approvals` rows and the transitions between them;
delegates the actual work to `AgentService`, which owns runs and graphs.

Why this service orchestrates the agent rather than the other way round
-----------------------------------------------------------------------
`AgentService.run_calendar_agent` proposes and stops. It cannot execute — the
executor is only built on the resume path. Something has to sit above both halves
and own the *workflow*: propose, record the request, wait, then either resume or
cancel.

That workflow is human-in-the-loop, so it lives here. The alternative — teaching
`AgentService` about approvals — would put the thing being gated and the gate in
one class, which is the arrangement this milestone exists to avoid.

No repository, deliberately
---------------------------
`docs/architecture.md` gives repositories to "query construction for complex
aggregates". These queries are a lookup by id and a filter by status; a repository
would be a file whose every method forwards its arguments. `ChunkRepository` earns
its existence with pgvector SQL that is dangerous to get wrong. This does not.

The three rules that make an approval mean something
----------------------------------------------------
**Only a pending approval can be decided.** The transition is the idempotency key,
and it has to be, because the decision arrives from a browser and browsers retry: a
double-clicked "approve" must create one event, not two.

**An expired approval cannot be decided at all.** The action was composed against
facts that were true when it was proposed. Executing it a week later runs it
against facts that may not be.

**Rejecting cancels the run.** A run left `paused_for_approval` after a rejection
is a run that looks resumable forever — and `resume_approved_run` checks exactly
that status, so it would be genuinely resumable by anyone who found it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.calendar.tools import describe
from app.agents.email.tools import describe as describe_email
from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError
from app.integrations import OAuthRegistry
from app.llm.base import LLMProvider
from app.models.agent_run import AgentRun, RunStatus
from app.models.approval import Approval, ApprovalStatus
from app.rag.embeddings import EmbeddingProvider
from app.services.agent_service import AgentService

logger = structlog.get_logger(__name__)


class ApprovalService:
    """The human-in-the-loop workflow: propose, wait, act."""

    def __init__(
        self,
        session: AsyncSession,
        embedder: EmbeddingProvider,
        llm: LLMProvider,
        settings: Settings,
    ) -> None:
        self._session = session
        self._agents = AgentService(session, embedder, llm, settings)
        self._settings = settings

    # -- proposing --------------------------------------------------------

    async def propose_calendar_action(
        self, organization_id: uuid.UUID, user_id: uuid.UUID | None, instruction: str
    ) -> tuple[AgentRun, Approval | None]:
        """Run the calendar agent and record what it wants permission for.

        Returns `(run, None)` when the agent understood nothing — a finished run with
        no approval, because asking somebody to permit nothing wastes their attention
        and teaches them to click through the next one.
        """
        run, action = await self._agents.run_calendar_agent(
            organization_id, instruction, user_id=user_id
        )

        if action is None:
            return run, None

        # The summary is rendered from the action by code, so the sentence somebody
        # reads is a faithful description of the thing that will execute rather than
        # a second account of it that might not match.
        return run, await self._record(organization_id, run, action, describe(action))

    async def propose_email_action(
        self, organization_id: uuid.UUID, user_id: uuid.UUID | None, instruction: str
    ) -> tuple[AgentRun, Approval | None]:
        """Run the email agent and record what it wants permission to send.

        **This is M12's deferred half**, and adding it required no change to
        anything in this class except a second method that differs from the first
        in two lines — which is what ADR-0015 claimed and had not yet demonstrated.

        The stakes are higher than the calendar's, and the code is identical, which
        is the point: the guarantee comes from the shape (the executor is built only
        on the resume path) rather than from anybody remembering that email is
        different.
        """
        run, action = await self._agents.run_email_agent(
            organization_id, instruction, user_id=user_id
        )

        if action is None:
            return run, None

        return run, await self._record(organization_id, run, action, describe_email(action))

    async def _record(
        self,
        organization_id: uuid.UUID,
        run: AgentRun,
        action: dict[str, Any],
        summary: str,
    ) -> Approval:
        """Write the row a human will decide on."""
        approval = Approval(
            agent_run_id=run.id,
            organization_id=organization_id,
            requested_action=action,
            summary=summary,
            status=ApprovalStatus.PENDING,
            expires_at=datetime.now(UTC) + timedelta(hours=self._settings.approval_ttl_hours),
        )
        self._session.add(approval)
        await self._session.flush()

        logger.info(
            "approval.requested",
            approval_id=str(approval.id),
            run_id=str(run.id),
            organization_id=str(organization_id),
            kind=action.get("kind"),
        )
        return approval

    # -- reading ----------------------------------------------------------

    async def get(self, organization_id: uuid.UUID, approval_id: uuid.UUID) -> Approval:
        """One approval, or `NotFoundError` if it is another tenant's.

        404 rather than 403 for a row that exists elsewhere — the same rule as every
        other resource here, and it matters more for this one: a distinct status
        would confirm that a specific approval id exists in *some* organization.
        """
        approval = await self._session.scalar(
            select(Approval).where(
                Approval.organization_id == organization_id, Approval.id == approval_id
            )
        )

        if approval is None:
            message = "Approval not found."
            raise NotFoundError(message)

        return approval

    async def list_pending(self, organization_id: uuid.UUID, *, limit: int = 50) -> list[Approval]:
        """The inbox: what is waiting for a human, oldest first.

        Oldest first, unlike every other listing in this API. A queue of things
        people are waiting on is worked from the front — newest-first would bury the
        request that has been outstanding longest, which is precisely the one about
        to expire.

        Rows whose clock has run out are filtered here rather than trusted from
        `status`, because nothing writes `EXPIRED` until a sweep runs. Showing an
        expired approval in an inbox invites somebody to click a button that will
        then refuse them.
        """
        approvals = await self._session.scalars(
            select(Approval)
            .where(
                Approval.organization_id == organization_id,
                Approval.status == ApprovalStatus.PENDING,
                Approval.expires_at > datetime.now(UTC),
            )
            .order_by(Approval.created_at.asc())
            .limit(limit)
        )
        return list(approvals)

    # -- deciding ---------------------------------------------------------

    async def approve(
        self,
        organization_id: uuid.UUID,
        approval_id: uuid.UUID,
        user_id: uuid.UUID | None,
        registry: OAuthRegistry,
    ) -> Approval:
        """Permit the action, then execute it.

        The order is deliberate and the opposite of the intuitive one: the row moves
        to `APPROVED` *before* the side effect runs. If execution then fails, there
        is a record saying a human authorised this and the attempt failed — which is
        the truth. Marking it approved only on success would make a failed execution
        indistinguishable from a decision nobody made.
        """
        approval = await self._decidable(organization_id, approval_id)

        approval.status = ApprovalStatus.APPROVED
        approval.decided_by = user_id
        approval.decided_at = datetime.now(UTC)
        # **Committed, not merely flushed**, and a test caught the difference.
        #
        # `AgentService._finish_failed` rolls the session back before recording a
        # failure — it has to, because the session may be in a failed state. A
        # decision that existed only in this uncommitted transaction went back with
        # it, leaving the run FAILED and the approval PENDING: an inbox item nobody
        # could ever action, because resuming checks the run's status and would
        # refuse it forever.
        #
        # The same lesson M9 learned about run rows ("committed before the graph
        # starts, deliberately"), reached from the other direction: a record of a
        # human decision must survive the failure of the thing it authorised.
        await self._session.commit()

        logger.info("approval.approved", approval_id=str(approval.id), decided_by=str(user_id))
        await self._agents.resume_approved_run(
            organization_id,
            approval.agent_run_id,
            registry,
            # The row's own copy of the action, handed to the resume path so it can
            # be checked against the checkpoint's. ADR-0015 said the two are
            # "identical by construction"; M14 put a registry lookup between the
            # approval and the executor, so the invariant is now enforced rather
            # than merely true.
            dict(approval.requested_action),
        )

        # Re-read: resuming commits, and a commit expires every ORM object in the
        # session. Returning the stale instance would raise on first attribute access
        # during serialisation — the `MissingGreenlet` M9 paid for twice.
        return await self.get(organization_id, approval_id)

    async def reject(
        self,
        organization_id: uuid.UUID,
        approval_id: uuid.UUID,
        user_id: uuid.UUID | None,
        reason: str | None = None,
    ) -> Approval:
        """Refuse the action and cancel the run that was waiting on it."""
        approval = await self._decidable(organization_id, approval_id)

        approval.status = ApprovalStatus.REJECTED
        approval.decided_by = user_id
        approval.decided_at = datetime.now(UTC)
        approval.reason = reason
        await self._cancel_run(approval.agent_run_id, "The action was rejected.")
        await self._session.flush()

        logger.info("approval.rejected", approval_id=str(approval.id), decided_by=str(user_id))
        return approval

    async def expire_overdue(self, organization_id: uuid.UUID) -> int:
        """Mark past-their-window approvals expired, and cancel their runs.

        Called on demand rather than by a scheduler, and that is a real gap stated
        plainly: nothing runs this on a timer yet. It matters less than it looks,
        because `list_pending` already filters by the clock and `_decidable` already
        refuses an expired row — so an unswept approval is invisible and
        unactionable, merely untidy.
        """
        overdue = await self._session.scalars(
            select(Approval).where(
                Approval.organization_id == organization_id,
                Approval.status == ApprovalStatus.PENDING,
                Approval.expires_at <= datetime.now(UTC),
            )
        )

        expired = 0

        for approval in overdue:
            approval.status = ApprovalStatus.EXPIRED
            await self._cancel_run(approval.agent_run_id, "Nobody approved this in time.")
            expired += 1

        await self._session.flush()

        if expired:
            logger.info("approval.expired", organization_id=str(organization_id), count=expired)

        return expired

    # -- internals --------------------------------------------------------

    async def _decidable(self, organization_id: uuid.UUID, approval_id: uuid.UUID) -> Approval:
        """The approval, if it can still be decided on.

        Both guards raise `ConflictError` — 409, "the resource is not in a state
        where this makes sense" — rather than 400 or 404. A client that
        double-clicked gets a status telling it the decision already happened, which
        is actionable; a 404 would suggest the approval never existed.
        """
        approval = await self.get(organization_id, approval_id)

        if not approval.is_pending:
            message = f"This request was already {approval.status.value}."
            raise ConflictError(message)

        if approval.is_expired():
            # Refused even though `status` still says pending, because expiry is a
            # fact about the clock and nothing has swept it yet.
            message = "This request expired before it was decided."
            raise ConflictError(message)

        return approval

    async def _cancel_run(self, run_id: uuid.UUID, reason: str) -> None:
        """Take a paused run out of limbo.

        `CANCELLED` has existed on `RunStatus` since M9 and this is its first writer.
        Leaving the run `paused_for_approval` after a decision would make it look
        resumable forever — and `resume_approved_run` checks exactly that status, so
        a stale row would be genuinely resumable by anyone who found it.
        """
        run = await self._session.get(AgentRun, run_id)

        if run is None:  # pragma: no cover — the foreign key guarantees it
            return

        run.status = RunStatus.CANCELLED
        run.error = reason
        run.finished_at = datetime.now(UTC)
        # Cleared for the same reason `resume_approved_run` clears it: a checkpoint
        # behind a terminal status is an invitation to resume something finished.
        run.checkpoint = None
