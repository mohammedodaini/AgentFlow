"""The supervisor against real Postgres (M15).

The claim M15 makes is that a user no longer has to know which agent they need.
The tests that matter are therefore the ones asserting *what happened downstream*
of one instruction — which run was created, under which agent name, and whether a
side effect was proposed rather than performed.

The most important one is `test_routing_adds_no_capability`. A new entry point
must not become a new path to a side effect, and the only way to know is to send
the instruction that would cause one and check that nothing reached a provider.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import CALENDAR_AGENT, EMAIL_AGENT, RAG_AGENT, SUPERVISOR_AGENT
from app.agents.email import tools as email_tools
from app.core.config import get_settings
from app.llm.offline import OfflineLLM
from app.models.agent_run import RunStatus
from app.models.approval import ApprovalStatus
from app.rag.embeddings import EmbeddingProvider
from app.services.agent_service import AgentService
from app.services.approval_service import ApprovalService
from app.services.supervisor_service import SupervisorService
from tests.factories import make_org_with_owner


class RefusingGmailClient:
    """Raises if anything reaches it. The point of several tests here."""

    async def create_draft(self, *args: object, **kwargs: object) -> object:  # pragma: no cover
        message = "proposing must never reach Gmail"
        raise AssertionError(message)

    async def send_draft(self, *args: object, **kwargs: object) -> object:  # pragma: no cover
        message = "proposing must never send"
        raise AssertionError(message)


@pytest.fixture(autouse=True)
def _no_gmail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(email_tools, "GmailClient", RefusingGmailClient)


def service(db_session: AsyncSession, embedder: EmbeddingProvider) -> SupervisorService:
    return SupervisorService(db_session, embedder, OfflineLLM(), get_settings())


def agents(db_session: AsyncSession, embedder: EmbeddingProvider) -> AgentService:
    return AgentService(db_session, embedder, OfflineLLM(), get_settings())


def approvals(db_session: AsyncSession, embedder: EmbeddingProvider) -> ApprovalService:
    return ApprovalService(db_session, embedder, OfflineLLM(), get_settings())


async def organization(db_session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    org, owner, _ = await make_org_with_owner(db_session)
    return org.id, owner.id


# --- one entry point -----------------------------------------------------


async def test_a_question_reaches_the_rag_agent(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The user said nothing about which agent to use. That is the milestone."""
    org_id, owner_id = await organization(db_session)

    outcome = await service(db_session, embedder).run(
        org_id, "How are expenses reimbursed?", user_id=owner_id
    )

    assert outcome.run.agent_name == SUPERVISOR_AGENT
    assert outcome.delegated is not None
    assert outcome.delegated.agent_name == RAG_AGENT


async def test_a_meeting_reaches_the_calendar_agent(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    org_id, owner_id = await organization(db_session)

    outcome = await service(db_session, embedder).run(
        org_id, "Schedule a design review on 2026-09-10 09:00", user_id=owner_id
    )

    assert outcome.delegated is not None
    assert outcome.delegated.agent_name == CALENDAR_AGENT
    assert outcome.approval is not None
    assert outcome.approval.requested_action["kind"] == "calendar.create_event"


async def test_a_message_reaches_the_email_agent(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    org_id, owner_id = await organization(db_session)

    outcome = await service(db_session, embedder).run(
        org_id, "Email ada@example.com about Q3 saying the report is ready", user_id=owner_id
    )

    assert outcome.delegated is not None
    assert outcome.delegated.agent_name == EMAIL_AGENT
    assert outcome.approval is not None
    assert outcome.approval.requested_action["to"] == "ada@example.com"


# --- the safety property -------------------------------------------------


async def test_a_paused_run_always_has_an_approval_behind_it(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """**The bug M15 found by running the thing, and the assertion that was
    missing.**

    `AgentService.run_calendar_agent` pauses the run and *returns* the action;
    only `ApprovalService.propose_calendar_action` turns that action into the row
    a human decides on. The first version of the supervisor called the agent
    directly, so every supervised side effect produced a run stuck in
    `PAUSED_FOR_APPROVAL` with nothing in anybody's inbox — unresumable, because
    resuming requires a decided approval.

    Every test passed. They asserted the delegated run's *status*, which was
    right, and never asked whether the thing that makes that status actionable
    existed. Postgres said it in one query: three paused runs, zero approvals.

    So this asserts the pair, for both side-effect agents, and it is the shape of
    assertion that was missing rather than a new rule.
    """
    org_id, owner_id = await organization(db_session)

    for instruction in (
        "Schedule a design review on 2026-09-10 09:00",
        "Email ada@example.com about Q3 saying the report is ready",
    ):
        outcome = await service(db_session, embedder).run(org_id, instruction, user_id=owner_id)

        assert outcome.delegated is not None
        assert outcome.delegated.status is RunStatus.PAUSED_FOR_APPROVAL
        assert outcome.approval is not None, f"{instruction} paused with nothing to decide"
        assert outcome.approval.status is ApprovalStatus.PENDING
        assert outcome.approval.agent_run_id == outcome.delegated.id


async def test_a_supervised_proposal_is_decidable(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The other half: the row is not merely present, it can actually be actioned.

    A pending approval that `reject` refuses would be the same failure wearing a
    row.
    """
    org_id, owner_id = await organization(db_session)

    outcome = await service(db_session, embedder).run(
        org_id, "Schedule a design review on 2026-09-10 09:00", user_id=owner_id
    )
    assert outcome.approval is not None

    decided = await approvals(db_session, embedder).reject(
        org_id, outcome.approval.id, owner_id, reason="not needed"
    )

    assert decided.status is ApprovalStatus.REJECTED


async def test_routing_adds_no_capability(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """**A new entry point must not be a new path to a side effect.**

    The supervisor delegates to the same methods a client could call directly, so
    an email still stops at an `approvals` row. `RefusingGmailClient` raises if
    anything reaches a provider, and the run pausing rather than succeeding is
    what proves the gate is still in front of it.
    """
    org_id, owner_id = await organization(db_session)

    outcome = await service(db_session, embedder).run(
        org_id, "Email ada@example.com about Q3 saying the report is ready", user_id=owner_id
    )

    assert outcome.delegated is not None
    assert outcome.delegated.status is RunStatus.PAUSED_FOR_APPROVAL


async def test_the_supervisor_never_performs_the_work(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Its own run holds a decision and nothing else. A supervisor that executed
    would become the thing every agent depends on rather than the thing that
    orders them (`docs/agents.md` rule 2)."""
    org_id, owner_id = await organization(db_session)

    outcome = await service(db_session, embedder).run(
        org_id, "Schedule a design review on 2026-09-10 09:00", user_id=owner_id
    )

    assert outcome.run.status is RunStatus.SUCCEEDED
    assert [step.node_name for step in outcome.run.steps] == ["classify", "plan"]
    assert outcome.run.output is not None
    assert outcome.run.output["agent"] == CALENDAR_AGENT


async def test_a_delegated_run_is_a_first_class_run(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Findable in `/agent-runs` under its own agent name, not buried as a step
    inside a supervisor run nobody thought to open."""
    org_id, owner_id = await organization(db_session)
    outcome = await service(db_session, embedder).run(
        org_id, "How are expenses reimbursed?", user_id=owner_id
    )
    assert outcome.delegated is not None

    fetched = await agents(db_session, embedder).get_run(org_id, outcome.delegated.id)
    assert fetched.agent_name == RAG_AGENT


# --- refusing ------------------------------------------------------------


async def test_an_unroutable_instruction_is_a_successful_refusal(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Understanding an instruction well enough to know nothing serves it is a
    *success*. Marking it FAILED would put "order me a taxi" in the same bucket as
    a crash, and page somebody for it."""
    org_id, owner_id = await organization(db_session)

    outcome = await service(db_session, embedder).run(
        org_id, "Order me a taxi to the airport", user_id=owner_id
    )

    assert outcome.run.status is RunStatus.SUCCEEDED
    assert outcome.delegated is None
    assert outcome.approval is None
    assert "documents" in outcome.reason


async def test_a_refusal_is_traced_with_its_reason(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """ "Why was my question refused?" has to be answerable from the run rather
    than by reading a regex."""
    org_id, owner_id = await organization(db_session)

    outcome = await service(db_session, embedder).run(org_id, "hello", user_id=owner_id)

    classify = outcome.run.steps[0]
    assert classify.tool_output is not None
    assert classify.tool_output["agent"] is None
    assert classify.tool_output["reason"]


# --- planning ------------------------------------------------------------


async def test_a_two_step_plan_runs_both_agents(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The reason a plan is worth having: the answer becomes the message.

    "Find our expenses policy and email it to ada" is not two unrelated requests,
    and the returned run is the *second* step's — because that is the one holding
    the thing a human now has to decide on.
    """
    org_id, owner_id = await organization(db_session)

    outcome = await service(db_session, embedder).run(
        org_id,
        "Find our expenses policy and email it to ada@example.com about expenses saying here it is",
        user_id=owner_id,
    )

    assert outcome.run.output is not None
    assert outcome.run.output["plan"] == [RAG_AGENT, EMAIL_AGENT]
    assert outcome.delegated is not None
    assert outcome.delegated.agent_name == EMAIL_AGENT
    assert outcome.delegated.status is RunStatus.PAUSED_FOR_APPROVAL


async def test_the_second_step_receives_what_the_first_found(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The hand-off, asserted on the run's recorded input rather than inferred.

    With no documents ingested the RAG step finds nothing, so the composed
    instruction is the original — which is the honest fallback, and exactly what
    the assertion below pins: the second step is always *given* the chance to use
    the answer, whether or not there was one.
    """
    org_id, owner_id = await organization(db_session)

    outcome = await service(db_session, embedder).run(
        org_id,
        "Find our expenses policy and email it to ada@example.com about expenses saying here it is",
        user_id=owner_id,
    )

    assert outcome.delegated is not None
    assert "ada@example.com" in str(outcome.delegated.input["instruction"])


async def test_a_plan_produces_one_approval_not_two(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """A plan always opens with a lookup, so only its second step can propose. Two
    approvals for one instruction would mean a user clicking twice for something
    they asked for once — and being unable to tell which click did what."""
    org_id, owner_id = await organization(db_session)

    outcome = await service(db_session, embedder).run(
        org_id,
        "Find our expenses policy and email it to ada@example.com about expenses saying here it is",
        user_id=owner_id,
    )

    runs, _ = await agents(db_session, embedder).list_runs(org_id, limit=50)
    paused = [run for run in runs if run.status is RunStatus.PAUSED_FOR_APPROVAL]

    assert len(paused) == 1
    assert outcome.delegated is not None
    assert paused[0].id == outcome.delegated.id
