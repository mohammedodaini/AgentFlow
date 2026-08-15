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
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import RAG_AGENT
from app.agents.history import HistoryTurn
from app.agents.rag.graph import build_graph
from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.llm.base import LLMError, LLMProvider
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
            await self._finish_failed(run_id, steps, error.message)
            raise
        except Exception:
            log.exception("agent.run_errored")
            await self._finish_failed(run_id, steps, "The agent failed unexpectedly. Try again.")
            raise

        usage = state.get("usage", {})
        total_tokens = usage.get("input", 0) + usage.get("output", 0)

        await self._runs.add_steps(run, steps)
        await self._runs.finish(
            run,
            RunStatus.SUCCEEDED,
            output={"answer": state.get("answer", ""), "citations": state.get("citations", [])},
            total_tokens=total_tokens,
            # Zero until M12, which owns pricing. A made-up figure here would be
            # worse than none: it would appear in reports, get trusted, and be
            # wrong by whatever margin the guessed rate was off.
            cost_usd=Decimal(0),
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

    async def _finish_failed(
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
