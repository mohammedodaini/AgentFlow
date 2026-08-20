"""Running an agent, and recording what it did.

Layer: services. The only caller of `app/agents/`, and the reason routes never
touch a graph directly: this owns the run row, the trace and the transaction.

The ordering here is the whole design
-------------------------------------
1. **Open the run row, then commit it** — before the graph starts. A row that
   only appears on success cannot record a crash, and "the agent hung" is
   exactly when someone needs a row to look at.
2. **Execute the graph**, collecting steps in memory.
3. **Persist the trace and the terminal state**, whatever happened — including
   on failure, where the trace is the most valuable thing produced.

Step 3 runs on the failure path too, not only the happy one. A failed run with
no trace is an outage with no evidence, which is the state this whole table
exists to prevent.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, cast

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import CALENDAR_AGENT, EMAIL_AGENT, RAG_AGENT
from app.agents.calendar.graph import (
    CalendarState,
    build_propose_graph,
    checkpoint_of,
    initial_state,
)
from app.agents.email.graph import EmailState
from app.agents.email.graph import build_propose_graph as build_email_propose_graph
from app.agents.email.graph import checkpoint_of as email_checkpoint_of
from app.agents.email.graph import initial_state as email_initial_state
from app.agents.execution import ExecutionState, action_kind, build_execute_graph
from app.agents.history import HistoryTurn
from app.agents.rag.graph import build_graph
from app.core.config import Settings
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.integrations import OAuthRegistry
from app.llm.base import LLMError, LLMProvider
from app.llm.pricing import cost_of
from app.models.agent_run import AgentRun, RunStatus
from app.rag.embeddings import EmbeddingProvider
from app.repositories.agent_run_repository import AgentRunRepository

logger = structlog.get_logger(__name__)


class AgentService:
    """Executes an agent and leaves a record of it."""

    def __init__(
        self,
        session: AsyncSession,
        embedder: EmbeddingProvider,
        llm: LLMProvider,
        settings: Settings,
    ) -> None:
        self._session = session
        self._runs = AgentRunRepository(session)
        self._embedder = embedder
        self._llm = llm
        self._settings = settings

    async def run_rag_agent(
        self,
        organization_id: uuid.UUID,
        question: str,
        *,
        user_id: uuid.UUID | None = None,
        top_k: int | None = None,
        conversation_id: uuid.UUID | None = None,
        history: list[HistoryTurn] | None = None,
    ) -> AgentRun:
        """Answer a question through the RAG graph, fully traced.

        `history` is passed in rather than loaded here, and the boundary is
        deliberate: this service owns *execution*, `ConversationService` owns the
        thread. A run invoked from `POST /agent-runs` has neither, and that has
        to stay expressible without this method knowing anything about
        conversations beyond an id it records.
        """
        payload: dict[str, Any] = {
            "question": question,
            "top_k": top_k or self._settings.retrieval_top_k,
            # Recorded so a run can be replayed with the context it actually had.
            # Without it, replaying a follow-up would search for "how much is
            # it?" against an empty thread and legitimately find nothing — and
            # the replay would look like a retrieval bug rather than a missing
            # input.
            "history_turns": len(history or []),
        }
        run = await self._runs.create(
            organization_id=organization_id,
            agent_name=RAG_AGENT,
            payload=payload,
            triggered_by=user_id,
            conversation_id=conversation_id,
        )
        # Committed before the graph starts, deliberately. Without this the row
        # exists only inside an uncommitted transaction, so a process killed
        # mid-graph leaves no trace of a run that definitely happened.
        await self._session.commit()

        # Captured *before* the graph runs, because `_finish_failed` rolls the
        # session back and a rollback expires every ORM object regardless of
        # `expire_on_commit`. Reading `run.id` afterwards would trigger a lazy
        # refresh, and a lazy refresh outside a greenlet raises `MissingGreenlet`
        # from inside the error handler — turning a reportable model outage into
        # an unhandled exception with no run record at all.
        run_id = run.id
        steps: list[dict[str, Any]] = []
        log = logger.bind(run_id=str(run_id), organization_id=str(organization_id))

        try:
            graph = build_graph(
                self._session,
                self._embedder,
                self._llm,
                self._settings,
                organization_id,
                steps.append,
            )
            state = await graph.ainvoke(
                {
                    "question": question,
                    "organization_id": organization_id,
                    "user_id": user_id,
                    "attempts": 0,
                    # Plain dicts, not `HistoryTurn` objects. State is
                    # checkpointed as JSONB, and a dataclass that cannot round
                    # trip through JSON is a run that cannot resume — the
                    # failure M12 would discover, not this milestone.
                    "history": [
                        {"role": turn.role, "content": turn.content} for turn in history or []
                    ],
                }
            )
        except LLMError as error:
            # A known, explicable failure: the model is down. The trace is
            # written anyway — it shows how far the graph got, which is the
            # difference between "retrieval worked and generation failed" and
            # "nothing ran at all".
            log.warning("agent.run_failed", reason=error.code)
            await self.finish_failed(run_id, steps, error.message)
            raise
        except Exception:
            log.exception("agent.run_errored")
            await self.finish_failed(run_id, steps, "The agent failed unexpectedly. Try again.")
            raise

        usage = state.get("usage", {})
        total_tokens = usage.get("input", 0) + usage.get("output", 0)

        await self._runs.add_steps(run, steps)
        await self._runs.finish(
            run,
            RunStatus.SUCCEEDED,
            output={"answer": state.get("answer", ""), "citations": state.get("citations", [])},
            total_tokens=total_tokens,
            # M12 owns pricing, and this is it: a real figure derived from the token
            # counts the steps recorded, at whatever rates the operator configured.
            # With no rates configured it is still `0.000000` — which now means
            # "nobody has told this system what it pays" rather than "we have not
            # built this yet", and `app/llm/pricing.py` says so.
            cost_usd=self._cost(usage),
        )
        await self._session.commit()

        log.info(
            "agent.run_succeeded",
            steps=len(steps),
            attempts=state.get("attempts", 0),
            citations=len(state.get("citations", [])),
            total_tokens=total_tokens,
        )

        # Re-read through the repository so `steps` arrives eagerly loaded.
        # Returning `run` directly looks equivalent and is not: the relationship
        # was never loaded, so the caller's first `run.steps` is a lazy load, and
        # a lazy load under asyncio raises `MissingGreenlet` — from inside
        # response serialisation, mentioning none of our code.
        return await self.get_run(organization_id, run_id)

    async def run_calendar_agent(
        self,
        organization_id: uuid.UUID,
        instruction: str,
        *,
        user_id: uuid.UUID | None = None,
    ) -> tuple[AgentRun, dict[str, Any] | None]:
        """Propose a calendar action and stop. **Never executes anything.**

        Returns the run and the proposed action, or `(run, None)` when the
        instruction could not be understood. The caller — `ApprovalService` — turns
        a proposed action into the row a human decides on.

        The split matters more than it looks. This method is the only way to reach
        the calendar agent, and it *cannot* create an event: the executor is built on
        the resume path and nowhere else. So "did the agent write to somebody's
        calendar without permission?" is answerable by reading one function rather
        than by auditing a branch.
        """
        payload: dict[str, Any] = {"instruction": instruction}
        run = await self._runs.create(
            organization_id=organization_id,
            agent_name=CALENDAR_AGENT,
            payload=payload,
            triggered_by=user_id,
        )
        # Committed before the graph starts, as at M9: a row that only appears on
        # success cannot record a crash.
        await self._session.commit()

        run_id = run.id
        steps: list[dict[str, Any]] = []
        log = logger.bind(run_id=str(run_id), organization_id=str(organization_id))

        try:
            # `ainvoke` is typed as returning the framework's loose state union, so
            # the cast is what lets the strict helpers below keep their real
            # signatures rather than widening to `dict[str, Any]` and losing the
            # checkpoint contract at exactly the boundary that depends on it.
            state = cast(
                "CalendarState",
                await build_propose_graph(steps.append).ainvoke(
                    initial_state(
                        instruction=instruction, organization_id=organization_id, user_id=user_id
                    )
                ),
            )
        except Exception:
            log.exception("agent.calendar_errored")
            await self.finish_failed(run_id, steps, "The agent failed unexpectedly. Try again.")
            raise

        action = state.get("proposed_action")
        run = await self.reload(run_id)
        await self._runs.add_steps(run, steps)

        if action is None:
            # Nothing to approve. The run is *finished*, not paused — an approval
            # asking somebody to permit nothing is worse than no approval at all.
            await self._runs.finish(
                run,
                RunStatus.SUCCEEDED,
                output={"refusal": state.get("refusal", ""), "proposed": False},
            )
            await self._session.commit()
            log.info("agent.calendar_nothing_to_propose")
            return await self.get_run(organization_id, run_id), None

        # The pause. `checkpoint` and `PAUSED_FOR_APPROVAL` both arrived at M9 and
        # have been unwritten until now; this is what they were for.
        run.status = RunStatus.PAUSED_FOR_APPROVAL
        run.checkpoint = checkpoint_of(state)
        run.output = {"proposed": True, "summary": state.get("summary", "")}
        await self._session.flush()
        await self._session.commit()

        log.info("agent.calendar_awaiting_approval", summary=state.get("summary", ""))
        return await self.get_run(organization_id, run_id), action

    async def run_email_agent(
        self,
        organization_id: uuid.UUID,
        instruction: str,
        *,
        user_id: uuid.UUID | None = None,
    ) -> tuple[AgentRun, dict[str, Any] | None]:
        """Propose an email and stop. **Never sends anything.**

        The same contract as `run_calendar_agent`, and deliberately a separate
        method rather than a `agent_name` parameter on one: the two build different
        state, and a single method taking a discriminator would put a branch on the
        path between "an agent ran" and "a side effect happened" — which is the one
        place in this codebase where a branch is worth avoiding at the cost of
        thirty duplicated lines.
        """
        payload: dict[str, Any] = {"instruction": instruction}
        run = await self._runs.create(
            organization_id=organization_id,
            agent_name=EMAIL_AGENT,
            payload=payload,
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
                "EmailState",
                await build_email_propose_graph(steps.append).ainvoke(
                    email_initial_state(
                        instruction=instruction, organization_id=organization_id, user_id=user_id
                    )
                ),
            )
        except Exception:
            log.exception("agent.email_errored")
            await self.finish_failed(run_id, steps, "The agent failed unexpectedly. Try again.")
            raise

        action = state.get("proposed_action")
        run = await self.reload(run_id)
        await self._runs.add_steps(run, steps)

        if action is None:
            await self._runs.finish(
                run,
                RunStatus.SUCCEEDED,
                output={"refusal": state.get("refusal", ""), "proposed": False},
            )
            await self._session.commit()
            log.info("agent.email_nothing_to_propose")
            return await self.get_run(organization_id, run_id), None

        run.status = RunStatus.PAUSED_FOR_APPROVAL
        run.checkpoint = email_checkpoint_of(state)
        run.output = {"proposed": True, "summary": state.get("summary", "")}
        await self._session.flush()
        await self._session.commit()

        log.info("agent.email_awaiting_approval", summary=state.get("summary", ""))
        return await self.get_run(organization_id, run_id), action

    async def resume_approved_run(
        self,
        organization_id: uuid.UUID,
        run_id: uuid.UUID,
        registry: OAuthRegistry,
        approved_action: dict[str, Any],
    ) -> AgentRun:
        """Execute the action a human approved. The only path to a side effect.

        Reads its state from `agent_runs.checkpoint` rather than from memory, which
        is the entire point: the process that proposed this is very likely gone — a
        deploy, a restart, a weekend — and the run has to be resumable by whichever
        process happens to be alive when somebody clicks.

        **The tenant comes from the caller, never from the checkpoint.** The stored
        state carries an `organization_id` and it is deliberately not trusted: a
        checkpoint that could nominate its own tenant would be an injection surface
        that survives restarts.

        **`approved_action` is the row's copy, and it is checked against the
        checkpoint's.** M14 renamed this from `resume_calendar_run` and made it
        dispatch on the action's `kind`, which means the thing that executes is now
        chosen by data rather than by the method's name. ADR-0015 asserted that
        what was approved and what runs "are identical by construction"; with one
        kind and one method that was true by inspection. With a lookup in the
        middle it is worth *enforcing*, so a disagreement between the two stored
        copies stops the run instead of silently preferring one.
        """
        run = await self.get_run(organization_id, run_id)

        if run.status is not RunStatus.PAUSED_FOR_APPROVAL or run.checkpoint is None:
            # Guards the double-execute a browser will eventually attempt. The run's
            # status is the idempotency key: once it has moved on, there is nothing
            # here left to run.
            message = "This run is not waiting to be resumed."
            raise ConflictError(message)

        action = run.checkpoint.get("proposed_action")

        if action != approved_action:
            # Written together in one transaction from the same dict, so this cannot
            # happen today. It is checked because the consequence if it ever does is
            # that a human authorised one thing and another was performed — the
            # single failure this whole design exists to prevent, and not one to
            # discover from a support ticket.
            message = "The approved action no longer matches this run. It was not performed."
            raise ConflictError(message)

        known = action_kind(str(action.get("kind", "")))
        steps: list[dict[str, Any]] = []
        executor = known.build_executor(self._session, registry, self._settings, organization_id)
        log = logger.bind(run_id=str(run_id), organization_id=str(organization_id), kind=known.kind)

        try:
            final = await build_execute_graph(
                executor, steps.append, tool_name=known.tool_name
            ).ainvoke(ExecutionState(proposed_action=action))
        except AppError as error:
            # A revoked credential, a missing integration, the provider refusing the
            # write. The run fails and says why — and the approval stays decided,
            # because a human did approve it. What failed was the execution.
            log.warning("agent.execute_failed", reason=error.code)
            await self.finish_failed(run_id, steps, error.message)
            raise

        run = await self.reload(run_id)
        await self._runs.add_steps(run, steps)
        # The checkpoint is cleared once used. Keeping it would leave a
        # resumable-looking run behind a terminal status, and the next person to read
        # the column would reasonably wonder whether it still meant something.
        run.checkpoint = None
        await self._runs.finish(run, RunStatus.SUCCEEDED, output=dict(final.get("result", {})))
        await self._session.commit()

        log.info("agent.executed")
        return await self.get_run(organization_id, run_id)

    async def get_run(self, organization_id: uuid.UUID, run_id: uuid.UUID) -> AgentRun:
        """One run and its trace, or `NotFoundError` if it is another tenant's.

        Not 403 for a run that exists elsewhere — the same rule as every other
        resource here: a distinct status turns the endpoint into an oracle for
        enumerating ids across tenants.
        """
        run = await self._runs.get(organization_id, run_id)

        if run is None:
            message = "Agent run not found."
            raise NotFoundError(message)

        return run

    async def list_runs(
        self,
        organization_id: uuid.UUID,
        *,
        status: RunStatus | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[AgentRun], int]:
        """A page of runs and the total, for the page envelope."""
        return (
            await self._runs.list_for_organization(
                organization_id, status=status, limit=limit, offset=offset
            ),
            await self._runs.count_for_organization(organization_id, status=status),
        )

    def _cost(self, usage: dict[str, int]) -> Decimal:
        """What this run cost, at the configured rates.

        Derived from counts that were *measured* — the steps recorded them — rather
        than estimated from character lengths or model defaults. That distinction is
        the reason the number is worth storing at all.
        """
        return cost_of(
            input_tokens=usage.get("input", 0),
            output_tokens=usage.get("output", 0),
            input_rate=self._settings.llm_input_cost_per_mtok,
            output_rate=self._settings.llm_output_cost_per_mtok,
        )

    async def reload(self, run_id: uuid.UUID) -> AgentRun:
        """Re-fetch a run after a commit expired it.

        `commit()` expires every ORM object in the session, so the instance held
        across one is unusable without a refresh — the same fact that made
        `finish_failed` take an id rather than an object at M9. Reloading through
        `session.get` rather than the repository because the caller wants the row,
        not its eagerly-loaded trace.

        Public since M15: `SupervisorService` runs its own graph over the same
        session and needs both this and `finish_failed`, which makes them part of
        this service's API rather than its internals.
        """
        run = await self._session.get(AgentRun, run_id)

        if run is None:  # pragma: no cover — committed moments earlier
            message = "Agent run not found."
            raise NotFoundError(message)

        return run

    async def finish_failed(
        self, run_id: uuid.UUID, steps: list[dict[str, Any]], message: str
    ) -> None:
        """Record the failure and everything that led to it.

        Takes an **id, not the object**. The session may be in a failed state
        after an exception, so this rolls back first — and a rollback expires
        every ORM object in the session, whatever `expire_on_commit` says.
        Passing the `AgentRun` through would mean touching an expired attribute
        here, which triggers a lazy refresh, which raises `MissingGreenlet`
        under asyncio: the error handler would fail, masking the real error and
        leaving the run in `running` forever.

        Re-fetching after the rollback is what makes this safe. The row survives
        because it was committed before the graph started, which is the reason
        that first commit exists.
        """
        await self._session.rollback()

        run = await self._session.get(AgentRun, run_id)

        if run is None:  # pragma: no cover — the row was committed above
            logger.error("agent.run_row_missing", run_id=str(run_id))
            return

        await self._runs.add_steps(run, steps)
        await self._runs.finish(run, RunStatus.FAILED, error=message)
        await self._session.commit()
