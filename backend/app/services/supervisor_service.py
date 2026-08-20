"""One instruction in, the right agent's work out — including its approval.

Layer: services. The M15 entry point, and a service of its own for a reason found
at runtime rather than at design time.

**Why this is not a method on `AgentService`.**
It was, for about an hour, and it was broken. `AgentService.run_calendar_agent`
and `run_email_agent` *propose* — they pause the run and hand back the action —
and it is `ApprovalService` that turns that action into the `approvals` row a
human decides on. A supervisor that called the agent methods directly therefore
produced a run stuck in `PAUSED_FOR_APPROVAL` with **no approval row at all**:
nothing in anybody's inbox, nothing to click, and a run that could never be
resumed because resuming requires a decided approval.

Every test passed. They asserted the delegated run's *status*, which was exactly
right, and never asked whether the thing that makes that status actionable
existed. Postgres said it in one query: three paused runs, zero approvals.

That is M12's lesson from a third direction — a pause is only half a mechanism;
the row is the other half — and the fix is structural rather than careful. This
service composes `AgentService` and `ApprovalService` and routes side effects
through the one that writes rows, so the broken path is not merely unused, it is
unreachable.

`ApprovalService` cannot host this instead: it would have to answer questions,
which have nothing to do with approvals. And `AgentService` cannot import
`ApprovalService`, which already imports it. A third thing that composes both is
what the dependency direction was always going to require.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, cast

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import CALENDAR_AGENT, EMAIL_AGENT, RAG_AGENT, SUPERVISOR_AGENT
from app.agents.supervisor.graph import SupervisorState
from app.agents.supervisor.graph import build_graph as build_supervisor_graph
from app.agents.supervisor.graph import initial_state as supervisor_initial_state
from app.agents.supervisor.tools import Router, RuleRouter
from app.core.config import Settings
from app.core.exceptions import ConflictError
from app.llm.base import LLMProvider
from app.models.agent_run import AgentRun, RunStatus
from app.models.approval import Approval
from app.monitoring.metrics import MetricsRegistry
from app.rag.embeddings import EmbeddingProvider
from app.repositories.agent_run_repository import AgentRunRepository
from app.services.agent_service import AgentService
from app.services.approval_service import ApprovalService

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SupervisorOutcome:
    """What a supervised instruction produced.

    `delegated` is None only when the supervisor refused — a successful run with
    nothing downstream, not an error. `approval` is the row a human now has to
    decide on, present exactly when the specialist proposed a side effect; it is
    returned whole so a caller can render "here is what it wants to do" without a
    second request, and so that a client cannot forget the row exists.
    """

    run: AgentRun
    delegated: AgentRun | None
    approval: Approval | None
    reason: str


class SupervisorService:
    """Classify an instruction, delegate it, and record what needs permission."""

    def __init__(
        self,
        session: AsyncSession,
        embedder: EmbeddingProvider,
        llm: LLMProvider,
        settings: Settings,
        router: Router | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._session = session
        self._runs = AgentRunRepository(session)
        self._agents = AgentService(session, embedder, llm, settings, metrics=metrics)
        # One `AgentService`, shared. Two over the same session would work and
        # would mean two objects holding two repositories over one transaction —
        # untidy rather than wrong, and the kind of untidy that becomes wrong the
        # first time one of them caches something.
        self._approvals = ApprovalService(session, embedder, llm, settings, agents=self._agents)
        # Defaulted rather than required, so no route has to know that routing is
        # pluggable. It is still a seam: the real implementation of this is a
        # model deciding, and passing one in is how the evaluation harness and the
        # tests substitute a router without touching the graph.
        self._router: Router = router or RuleRouter()

    async def run(
        self,
        organization_id: uuid.UUID,
        instruction: str,
        *,
        user_id: uuid.UUID | None = None,
    ) -> SupervisorOutcome:
        """Decide who should act, then let them.

        **The supervisor never performs the work.** It classifies, records, and
        delegates to the same methods a client could have called directly. Three
        consequences worth stating:

        - the call graph stays a tree (`docs/agents.md` rule 2), so a trace reads
          top to bottom rather than as a conversation between components;
        - every safety property the specialists already had still holds, because
          nothing here is a new path to them — a calendar or email action still
          reaches a provider only through an `approvals` row somebody decided on;
        - and a delegated run is a *first-class* run, findable in `/agent-runs`
          under its own agent name rather than a hidden sub-step of this one.
        """
        run = await self._runs.create(
            organization_id=organization_id,
            agent_name=SUPERVISOR_AGENT,
            payload={"instruction": instruction},
            triggered_by=user_id,
        )
        # Committed before the graph starts, as at M9: a row that only appears on
        # success cannot record a crash.
        await self._session.commit()

        run_id = run.id
        steps: list[dict[str, Any]] = []
        log = logger.bind(run_id=str(run_id), organization_id=str(organization_id))

        try:
            state = cast(
                "SupervisorState",
                await build_supervisor_graph(self._router, steps.append).ainvoke(
                    supervisor_initial_state(
                        instruction=instruction,
                        organization_id=organization_id,
                        user_id=user_id,
                    )
                ),
            )
        except Exception:
            log.exception("agent.supervisor_errored")
            await self._agents.finish_failed(
                run_id, steps, "The agent failed unexpectedly. Try again."
            )
            raise

        plan = list(state.get("plan", []))
        reason = state.get("reason", "")

        run = await self._agents.reload(run_id)
        await self._runs.add_steps(run, steps)
        await self._agents.finish(
            run,
            RunStatus.SUCCEEDED,
            output={"agent": state.get("agent"), "plan": plan, "reason": reason},
        )
        await self._session.commit()

        supervisor_run = await self._agents.get_run(organization_id, run_id)

        if not plan:
            # A refusal is a *successful* supervisor run. It understood the
            # instruction well enough to know nothing here serves it, which is a
            # different outcome from failing — and marking it FAILED would put
            # "order me a taxi" in the same bucket as a crash, and page somebody.
            log.info("agent.supervisor_refused")
            return SupervisorOutcome(
                run=supervisor_run, delegated=None, approval=None, reason=reason
            )

        log.info("agent.supervisor_routed", plan=plan)
        delegated, approval = await self._run_plan(
            organization_id, plan, instruction, user_id=user_id
        )
        return SupervisorOutcome(
            run=supervisor_run, delegated=delegated, approval=approval, reason=reason
        )

    async def _run_plan(
        self,
        organization_id: uuid.UUID,
        plan: list[str],
        instruction: str,
        *,
        user_id: uuid.UUID | None,
    ) -> tuple[AgentRun, Approval | None]:
        """Execute an ordered plan, feeding each step's result into the next.

        Two steps at most today — see `agents/planner/graph.py` for why that
        ceiling is deliberate rather than unfinished.

        **The interesting part is the hand-off.** For a plan like `[rag, email]`
        the answer from the first step becomes part of the message proposed by the
        second, which is the entire reason the plan is worth having: "find our
        expenses policy and email it to ada" is not two unrelated requests. The
        composition happens here rather than in either agent, because neither
        should know it is part of a sequence.
        """
        run, approval = await self._delegate(organization_id, plan[0], instruction, user_id=user_id)

        if not plan[1:]:
            return run, approval

        # Only the RAG agent produces an `answer`, and a plan always opens with a
        # lookup — so the first step never proposes, and one instruction never
        # produces two approvals for a human to disentangle.
        answer = str((run.output or {}).get("answer", "")).strip()
        enriched = f"{instruction}\n\n{answer}" if answer else instruction

        return await self._delegate(organization_id, plan[1], enriched, user_id=user_id)

    async def _delegate(
        self,
        organization_id: uuid.UUID,
        agent: str,
        instruction: str,
        *,
        user_id: uuid.UUID | None,
    ) -> tuple[AgentRun, Approval | None]:
        """Hand the work to one specialist, by name.

        **The two side-effect agents go through `ApprovalService`, never through
        `AgentService` directly.** That is the fix described in the module
        docstring: the agent methods pause a run and return an action, and only
        `propose_*_action` turns that action into the row that makes the pause
        actionable. Calling the agent directly leaves a run nobody can resume.
        """
        if agent == RAG_AGENT:
            # No approval: answering proposes nothing, which is why the RAG agent
            # needs no permission and the other two do.
            run = await self._agents.run_rag_agent(organization_id, instruction, user_id=user_id)
            return run, None

        if agent == CALENDAR_AGENT:
            return await self._approvals.propose_calendar_action(
                organization_id, user_id, instruction
            )

        if agent == EMAIL_AGENT:
            return await self._approvals.propose_email_action(organization_id, user_id, instruction)

        # Unreachable through `RuleRouter`, which can only name agents that exist.
        # Present because the router is a seam, and a model-backed implementation
        # can name anything at all.
        message = f"No agent named {agent!r} can be run."
        raise ConflictError(message)
