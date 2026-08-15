"""Data access for `agent_runs` and `agent_steps`.

Layer: repositories. Every method takes an `organization_id` and every query
filters on it — the same non-negotiable rule as `DocumentRepository` (M5) and
`ChunkRepository` (M6). A repository method that *can* be called without a
tenant is one that eventually will be.

Steps are written here rather than by the graph, and that matters: the graph
collects what it did in memory and the service persists it in one transaction.
A graph writing its own rows would hold a database session inside every node,
which is both a resource problem and a correctness one — a run that failed
halfway would leave a partial trace committed with no run row to explain it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.agent_run import AgentRun, RunStatus
from app.models.agent_step import AgentStep


class AgentRunRepository:
    """Tenant-scoped reads and writes for agent execution records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        agent_name: str,
        payload: dict[str, Any],
        triggered_by: uuid.UUID | None,
        conversation_id: uuid.UUID | None = None,
    ) -> AgentRun:
        """Open a run in `running` state.

        Created *before* the graph executes, not after. A run row that only
        appears on success cannot record a crash — and "the agent hung" is
        precisely the case where somebody needs a row to look at.
        """
        run = AgentRun(
            organization_id=organization_id,
            agent_name=agent_name,
            triggered_by=triggered_by,
            conversation_id=conversation_id,
            status=RunStatus.RUNNING,
            input=payload,
            started_at=datetime.now(UTC),
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def add_steps(self, run: AgentRun, steps: list[dict[str, Any]]) -> None:
        """Append the trace, numbering the steps in execution order.

        `step_index` is assigned here from list position rather than by each
        node, because a node does not know how many ran before it — and a
        counter threaded through graph state would be one more thing a
        conditional edge could get wrong.
        """
        self._session.add_all(
            AgentStep(
                agent_run_id=run.id,
                step_index=index,
                node_name=step["node_name"],
                tool_name=step.get("tool_name"),
                tool_input=step.get("tool_input"),
                tool_output=step.get("tool_output"),
                latency_ms=step.get("latency_ms", 0),
                tokens=step.get("tokens", 0),
            )
            for index, step in enumerate(steps)
        )
        await self._session.flush()

    async def finish(
        self,
        run: AgentRun,
        status: RunStatus,
        *,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        total_tokens: int = 0,
        cost_usd: Decimal = Decimal(0),
    ) -> AgentRun:
        """Move a run to a terminal state and stamp what it cost."""
        run.status = status
        run.output = output
        run.error = error
        run.total_tokens = total_tokens
        run.cost_usd = cost_usd
        run.finished_at = datetime.now(UTC)
        await self._session.flush()
        return run

    async def get(self, organization_id: uuid.UUID, run_id: uuid.UUID) -> AgentRun | None:
        """One run with its steps, or None if it belongs to another tenant.

        `selectinload` rather than a lazy load: this is read in an async request
        and serialised immediately afterwards, and a lazy relationship under
        asyncio raises `MissingGreenlet` from inside serialisation — an error
        that mentions none of your own code.
        """
        run: AgentRun | None = await self._session.scalar(
            select(AgentRun)
            .where(AgentRun.organization_id == organization_id, AgentRun.id == run_id)
            .options(selectinload(AgentRun.steps))
        )
        return run

    async def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        status: RunStatus | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[AgentRun]:
        """A page of runs, newest first.

        Deliberately *without* their steps. A listing of twenty runs would drag
        every step of each across the wire to render a summary showing none of
        them — and the trace of a long run is the largest thing in this schema.
        """
        query = (
            select(AgentRun)
            .where(AgentRun.organization_id == organization_id)
            .order_by(AgentRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )

        if status is not None:
            query = query.where(AgentRun.status == status)

        return list(await self._session.scalars(query))

    async def count_for_organization(
        self, organization_id: uuid.UUID, *, status: RunStatus | None = None
    ) -> int:
        """Total matching runs, for the page envelope."""
        query = select(func.count(AgentRun.id)).where(AgentRun.organization_id == organization_id)

        if status is not None:
            query = query.where(AgentRun.status == status)

        return await self._session.scalar(query) or 0
