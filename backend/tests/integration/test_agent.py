"""The RAG agent: the graph, the trace, and the run record (M9).

Integration, because the interesting behaviour only exists when the graph talks
to a real retriever over real pgvector. Mocking retrieval would leave the
conditional edge — the one thing this milestone adds over `/ask` — asserting
against a fake's return value.

The tests split into two kinds, and both matter:

- **Did the graph take the right path?** The retry cycle is the feature. A test
  checking only the final answer would pass whether the rewrite ran or not.
- **Is the trace true?** A trace that omits a step, or misorders one, is worse
  than no trace: it is evidence that will be believed and is wrong.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import RAG_AGENT
from app.agents.rag.graph import GENERATE, MAX_ATTEMPTS, RETRIEVE, REWRITE
from app.agents.rag.tools import SEARCH_CHUNKS, build_search_chunks
from app.core.config import get_settings
from app.core.exceptions import NotFoundError
from app.llm.base import Completion, LLMError, LLMProvider
from app.llm.offline import NO_CONTEXT_ANSWER, OfflineLLM
from app.models.agent_run import AgentRun, RunStatus
from app.models.agent_step import AgentStep
from app.rag.embeddings import EmbeddingProvider
from app.services.agent_service import AgentService
from tests.factories import make_document, make_org_with_owner
from tests.integration.test_retrieval import EXPENSES, HOLIDAY, PLANTS, index

UNANSWERABLE = "What is the pension contribution?"
"""A question asked of an organization with nothing indexed, so retrieval comes
back empty twice and the whole retry path runs.

An *empty corpus* rather than a merely off-topic question, and the reason is a
measurement rather than convenience. M8 established that the offline hashing
embedder scores almost any natural question non-zero against almost any
document, because ordinary words ("what", "is", "the") collide. So no realistic
question retrieves *nothing* from a non-empty corpus with this embedder, and a
test built on one would assert a path it never took.

A new organization with no uploads is a real scenario, it reaches the same edge,
and it does not depend on the embedder's quirks. What it cannot tell us is how
often the retry fires in production against a semantic embedder — that needs a
key, and M8's harness is where it would be measured.
"""


class BrokenLLM:
    """Fails on every call. Used to prove the trace survives a failure."""

    model = "broken"

    async def complete(self, *, system: str, prompt: str) -> Completion:
        del system, prompt
        message = "The language model is unavailable. Please try again."
        raise LLMError(message)

    async def stream(self, *, system: str, prompt: str) -> AsyncIterator[str]:
        del system, prompt
        message = "unused"
        raise LLMError(message)
        yield ""  # pragma: no cover — makes this an async generator


def service(
    session: AsyncSession,
    embedder: EmbeddingProvider,
    *,
    llm: LLMProvider | None = None,
    **overrides: object,
) -> AgentService:
    return AgentService(
        session, embedder, llm or OfflineLLM(), get_settings().model_copy(update=overrides)
    )


async def corpus(session: AsyncSession, embedder: EmbeddingProvider) -> tuple[uuid.UUID, uuid.UUID]:
    """An organization with a three-paragraph handbook. Returns (org, user)."""
    organization, user, _ = await make_org_with_owner(session)
    document = await make_document(session, organization=organization, title="handbook.pdf")
    await index(session, embedder, document, [EXPENSES, HOLIDAY, PLANTS])
    return organization.id, user.id


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


async def test_the_agent_answers_from_the_corpus_with_citations(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    organization_id, user_id = await corpus(db_session, embedder)

    run = await service(db_session, embedder).run_rag_agent(
        organization_id, "expenses receipt reimbursed", user_id=user_id
    )

    assert run.status is RunStatus.SUCCEEDED
    assert run.output is not None
    assert "reimbursed" in run.output["answer"]
    assert run.output["citations"]
    assert run.error is None


async def test_a_successful_run_records_who_asked_and_what(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """`input` is stored so a run can be *replayed*. Reconstructing the question
    from logs is guesswork the moment a prompt changes."""
    organization_id, user_id = await corpus(db_session, embedder)

    run = await service(db_session, embedder).run_rag_agent(
        organization_id, "how are expenses reimbursed?", user_id=user_id, top_k=3
    )

    assert run.agent_name == RAG_AGENT
    assert run.triggered_by == user_id
    assert run.input == {"question": "how are expenses reimbursed?", "top_k": 3}
    assert run.finished_at is not None
    assert run.duration_ms is not None


async def test_tokens_are_attributed_to_the_run(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The number M12's cost accounting aggregates. Capturing it from the first
    run means that milestone is a query rather than an archaeology project."""
    organization_id, _ = await corpus(db_session, embedder)

    run = await service(db_session, embedder).run_rag_agent(organization_id, "expenses")

    assert run.total_tokens > 0


# --------------------------------------------------------------------------
# the trace
# --------------------------------------------------------------------------


async def test_the_trace_records_every_node_in_order(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """A trace that omits or misorders a step is worse than none: it is evidence
    that will be believed and is wrong."""
    organization_id, _ = await corpus(db_session, embedder)

    run = await service(db_session, embedder).run_rag_agent(organization_id, "expenses receipt")

    steps = list(run.steps)
    assert [step.step_index for step in steps] == list(range(len(steps)))
    assert [step.node_name for step in steps] == [RETRIEVE, GENERATE]


async def test_the_retrieval_step_records_the_tool_and_its_result(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """`tool_name` is what makes "which runs used the retriever, and how slow
    was it?" a `WHERE` clause rather than a log grep."""
    organization_id, _ = await corpus(db_session, embedder)

    run = await service(db_session, embedder).run_rag_agent(organization_id, "expenses receipt")
    retrieval = next(step for step in run.steps if step.node_name == RETRIEVE)

    assert retrieval.tool_name == SEARCH_CHUNKS
    assert retrieval.tool_input is not None
    assert retrieval.tool_input["query"] == "expenses receipt"
    assert retrieval.tool_output is not None
    assert retrieval.tool_output["count"] > 0


async def test_the_trace_does_not_duplicate_the_corpus(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Chunk text is already in `document_chunks`. Copying it into every trace
    is how a trace table becomes the biggest one in the database."""
    organization_id, _ = await corpus(db_session, embedder)

    run = await service(db_session, embedder).run_rag_agent(organization_id, "expenses receipt")
    retrieval = next(step for step in run.steps if step.node_name == RETRIEVE)

    assert retrieval.tool_output is not None
    assert EXPENSES not in str(retrieval.tool_output)


async def test_steps_are_deleted_with_their_run(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """`ON DELETE CASCADE`, enforced by PostgreSQL. A step cannot outlive the
    run it explains."""
    organization_id, _ = await corpus(db_session, embedder)
    run = await service(db_session, embedder).run_rag_agent(organization_id, "expenses")

    await db_session.delete(run)
    await db_session.flush()

    remaining = await db_session.scalars(select(AgentStep).where(AgentStep.agent_run_id == run.id))
    assert remaining.all() == []


# --------------------------------------------------------------------------
# the retry cycle — the feature this milestone adds over /ask
# --------------------------------------------------------------------------


async def test_a_question_that_retrieves_nothing_is_rewritten_and_retried(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The graph's cycle, asserted by the *path taken* rather than the answer.

    A test checking only the final text would pass whether the rewrite ran or
    not. The trace is what distinguishes "answered on the first try" from
    "retrieved nothing, rewrote, and searched again".
    """
    empty, _, _ = await make_org_with_owner(db_session)

    run = await service(db_session, embedder).run_rag_agent(empty.id, UNANSWERABLE)

    nodes = [step.node_name for step in run.steps]

    assert REWRITE in nodes, nodes
    assert nodes.count(RETRIEVE) == MAX_ATTEMPTS, "retrieval must run again after the rewrite"


async def test_the_rewrite_strips_filler_and_records_the_new_query(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The rewrite is deterministic — `docs/agents.md`: if code can do it, code
    does it. The trace shows the query that produced the chunks, not the one the
    user typed."""
    empty, _, _ = await make_org_with_owner(db_session)

    run = await service(db_session, embedder).run_rag_agent(empty.id, UNANSWERABLE)
    rewrite = next(step for step in run.steps if step.node_name == REWRITE)

    assert rewrite.tool_output is not None
    assert rewrite.tool_output["search_query"] == "pension contribution"
    assert rewrite.tool_name is None, "the rewrite calls no tool"


async def test_the_cycle_is_bounded(db_session: AsyncSession, embedder: EmbeddingProvider) -> None:
    """A conditional edge routing back to `retrieve` is a cycle, and a cycle
    with no bound is a graph that can bill indefinitely."""
    empty, _, _ = await make_org_with_owner(db_session)

    run = await service(db_session, embedder).run_rag_agent(empty.id, UNANSWERABLE)

    assert [step.node_name for step in run.steps].count(RETRIEVE) <= MAX_ATTEMPTS


async def test_exhausting_the_retries_refuses_rather_than_returning_nothing(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Out of attempts with nothing found is not a failure. Routing to END would
    return an empty answer with no explanation instead of an honest refusal."""
    empty, _, _ = await make_org_with_owner(db_session)

    run = await service(db_session, embedder).run_rag_agent(empty.id, UNANSWERABLE)

    assert run.status is RunStatus.SUCCEEDED
    assert run.output is not None
    assert run.output["answer"] == NO_CONTEXT_ANSWER
    assert run.output["citations"] == []


# --------------------------------------------------------------------------
# failure
# --------------------------------------------------------------------------


async def test_a_failed_run_is_recorded_with_its_trace(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The most valuable trace there is.

    It shows how far the graph got — the difference between "retrieval worked
    and generation failed" and "nothing ran". A failed run with no trace is an
    outage with no evidence.
    """
    organization_id, _ = await corpus(db_session, embedder)

    with pytest.raises(LLMError):
        await service(db_session, embedder, llm=BrokenLLM()).run_rag_agent(
            organization_id, "expenses receipt"
        )

    run = await db_session.scalar(
        select(AgentRun).where(AgentRun.organization_id == organization_id)
    )
    assert run is not None
    assert run.status is RunStatus.FAILED
    assert run.error
    assert run.finished_at is not None


async def test_a_run_row_exists_even_though_the_graph_failed(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The row is committed before the graph starts, so a process killed
    mid-graph still leaves evidence that a run happened. The failure path is the
    only one that can observe this without the run completing."""
    organization_id, _ = await corpus(db_session, embedder)

    with pytest.raises(LLMError):
        await service(db_session, embedder, llm=BrokenLLM()).run_rag_agent(
            organization_id, "expenses"
        )

    runs = (
        await db_session.scalars(
            select(AgentRun).where(AgentRun.organization_id == organization_id)
        )
    ).all()
    assert len(runs) == 1


# --------------------------------------------------------------------------
# tenancy
# --------------------------------------------------------------------------


async def test_the_tool_cannot_be_pointed_at_another_tenant(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The security property the tool's design exists for.

    The organization is closed over at construction and absent from the schema
    the model sees, so no tool call — however the model was manipulated into
    making it — can name a different tenant. This asserts the closure holds by
    building the tool for one organization and finding the other's content
    unreachable through it.
    """
    await corpus(db_session, embedder)
    mine, _, _ = await make_org_with_owner(db_session)
    my_document = await make_document(db_session, organization=mine, title="ours.txt")
    await index(db_session, embedder, my_document, ["We ship on Fridays."])

    tool = build_search_chunks(db_session, embedder, mine.id)
    results = await tool.ainvoke({"query": "expenses receipt reimbursed", "top_k": 10})

    assert all(EXPENSES not in result["content"] for result in results)


async def test_another_tenants_run_is_not_readable(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    organization_id, _ = await corpus(db_session, embedder)
    run = await service(db_session, embedder).run_rag_agent(organization_id, "expenses")

    other, _, _ = await make_org_with_owner(db_session)

    with pytest.raises(NotFoundError):
        await service(db_session, embedder).get_run(other.id, run.id)


async def test_listing_is_scoped_and_paged(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    organization_id, _ = await corpus(db_session, embedder)
    agent = service(db_session, embedder)
    await agent.run_rag_agent(organization_id, "expenses")
    await agent.run_rag_agent(organization_id, "holiday")

    runs, total = await agent.list_runs(organization_id, limit=1)

    assert total == 2
    assert len(runs) == 1

    failed, count = await agent.list_runs(organization_id, status=RunStatus.FAILED)
    assert (failed, count) == ([], 0)
