"""Human-in-the-loop against real Postgres (M12).

The milestone's claim is that an approval survives the process that created it, so
the test that matters most is `test_a_paused_run_survives_a_restart`: it proposes
with one service instance, throws it away, and resumes with a completely fresh one.
If the pause lived in memory that test fails, and no other test here would notice.

The rest guard the three rules that make an approval mean anything: only a pending
one can be decided, an expired one cannot be decided at all, and rejecting cancels
the run rather than leaving it looking resumable forever.

Google is stubbed by replacing the client *in one module*. Patching `httpx` globally
would silence every HTTP call in the process — including ones a test never meant to
stub, so a real request that should have failed would quietly pass.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.calendar import tools as calendar_tools
from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError
from app.integrations import OAuthRegistry
from app.integrations.base import OAuthRevokedError
from app.integrations.google_calendar.client import CalendarEvent
from app.integrations.offline import OfflineOAuthProvider
from app.llm.offline import OfflineLLM
from app.models.agent_run import AgentRun, RunStatus
from app.models.approval import Approval, ApprovalStatus
from app.models.integration import Provider
from app.rag.embeddings import EmbeddingProvider
from app.services.approval_service import ApprovalService
from app.services.integration_service import IntegrationService
from tests.factories import make_org_with_owner
from tests.unit.test_oauth import code_from

INSTRUCTION = "Schedule a design review on 2026-08-20 09:00"
CALENDAR = Provider.GOOGLE_CALENDAR


class FakeCalendarClient:
    """Records what it was asked to create instead of calling Google.

    Class-scoped counters, because the sharpest question here is *how many times* the
    side effect happened — and a per-instance counter cannot answer it when the code
    under test constructs its own client.
    """

    created: list[dict[str, Any]] = []
    fail_with: Exception | None = None

    @classmethod
    def reset(cls) -> None:
        cls.created = []
        cls.fail_with = None

    async def create_event(
        self, access_token: str, *, summary: str, starts_at: Any, ends_at: Any, **_: Any
    ) -> CalendarEvent:
        del access_token

        if FakeCalendarClient.fail_with is not None:
            raise FakeCalendarClient.fail_with

        FakeCalendarClient.created.append({"summary": summary, "starts_at": starts_at})
        return CalendarEvent(
            event_id=f"evt-{len(FakeCalendarClient.created)}",
            title=summary,
            starts_at=starts_at,
            ends_at=ends_at,
            all_day=False,
            url="https://calendar.google.test/evt",
        )


@pytest.fixture(autouse=True)
def _stub_google(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the calendar client inside the tools module, and nowhere else."""
    FakeCalendarClient.reset()
    monkeypatch.setattr(calendar_tools, "GoogleCalendarClient", FakeCalendarClient)


@pytest.fixture
def registry() -> OAuthRegistry:
    return OAuthRegistry({CALENDAR: OfflineOAuthProvider(CALENDAR.value, ["calendar.events"])})


def service(db_session: AsyncSession, embedder: EmbeddingProvider) -> ApprovalService:
    """A *fresh* service over the same session.

    Built per call on purpose: the restart test needs two instances that share
    nothing but the database, which is the only thing a real restart preserves.
    """
    return ApprovalService(db_session, embedder, OfflineLLM(), get_settings())


async def connect_calendar(
    db_session: AsyncSession, redis_client: Redis, registry: OAuthRegistry, org: uuid.UUID
) -> None:
    """Give the organization a working credential, through the real OAuth flow."""
    integrations = IntegrationService(db_session, redis_client, registry, get_settings())
    pending = await integrations.begin_connect(org, None, CALENDAR)
    await integrations.complete_callback(
        CALENDAR, state=pending.state, code=code_from(pending.authorize_url)
    )


# --------------------------------------------------------------------------
# proposing
# --------------------------------------------------------------------------


async def test_proposing_pauses_the_run_and_touches_nothing(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The milestone in one test: the agent asks, and nothing happens yet.

    `PAUSED_FOR_APPROVAL` and `checkpoint` both arrived at M9 and have been unwritten
    until now.
    """
    organization, owner, _ = await make_org_with_owner(db_session)

    run, approval = await service(db_session, embedder).propose_calendar_action(
        organization.id, owner.id, INSTRUCTION
    )

    assert run.status is RunStatus.PAUSED_FOR_APPROVAL
    assert run.checkpoint is not None
    assert approval is not None
    assert approval.status is ApprovalStatus.PENDING
    assert FakeCalendarClient.created == [], "proposing must never create an event"


async def test_the_stored_action_is_what_will_execute(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """ "What was approved" and "what ran" are the same object by construction.

    Storing a *reference* and re-deriving the action at execution time would mean a
    user approved a summary and something else ran — a prompt change or a clock tick
    between the two is enough.
    """
    organization, owner, _ = await make_org_with_owner(db_session)

    _, approval = await service(db_session, embedder).propose_calendar_action(
        organization.id, owner.id, INSTRUCTION
    )

    assert approval is not None
    assert approval.requested_action["starts_at"] == "2026-08-20T09:00:00+00:00"
    assert "design review" in approval.summary


async def test_an_unparseable_instruction_asks_nobody_for_anything(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """A finished run and no approval. Asking somebody to permit nothing wastes their
    attention and teaches them to click through the next one."""
    organization, owner, _ = await make_org_with_owner(db_session)

    run, approval = await service(db_session, embedder).propose_calendar_action(
        organization.id, owner.id, "Schedule something tomorrow afternoon"
    )

    assert approval is None
    assert run.status is RunStatus.SUCCEEDED
    assert run.checkpoint is None
    assert await service(db_session, embedder).list_pending(organization.id) == []


# --------------------------------------------------------------------------
# the restart — the key risk
# --------------------------------------------------------------------------


async def test_a_paused_run_survives_a_restart(
    db_session: AsyncSession,
    embedder: EmbeddingProvider,
    redis_client: Redis,
    registry: OAuthRegistry,
) -> None:
    """**The assertion this milestone exists for.**

    The roadmap names the risk: "an approval that is only an in-memory interrupt does
    not survive a restart. It must be a row as well." The gap between proposing and
    clicking is hours, so a deploy in that window is the expected case, not an edge
    case.

    This proposes with one service, discards it entirely, and approves with a fresh
    one. The only thing carried across is the database — which is exactly what a
    restart preserves and what an in-process interrupt would not.
    """
    organization, owner, _ = await make_org_with_owner(db_session)
    await connect_calendar(db_session, redis_client, registry, organization.id)

    _, approval = await service(db_session, embedder).propose_calendar_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None
    approval_id = approval.id

    # Everything from before this line is gone: a new service, holding no memory of
    # the graph that paused.
    decided = await service(db_session, embedder).approve(
        organization.id, approval_id, owner.id, registry
    )

    assert decided.status is ApprovalStatus.APPROVED
    assert len(FakeCalendarClient.created) == 1
    assert "design review" in FakeCalendarClient.created[0]["summary"]


async def test_the_checkpoint_is_cleared_once_used(
    db_session: AsyncSession,
    embedder: EmbeddingProvider,
    redis_client: Redis,
    registry: OAuthRegistry,
) -> None:
    """A checkpoint behind a terminal status is an invitation to resume something
    finished — and `resume_calendar_run` checks exactly that pair."""
    organization, owner, _ = await make_org_with_owner(db_session)
    await connect_calendar(db_session, redis_client, registry, organization.id)

    _, approval = await service(db_session, embedder).propose_calendar_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None

    await service(db_session, embedder).approve(organization.id, approval.id, owner.id, registry)

    run = await db_session.get(AgentRun, approval.agent_run_id)
    assert run is not None
    assert run.status is RunStatus.SUCCEEDED
    assert run.checkpoint is None


# --------------------------------------------------------------------------
# deciding once
# --------------------------------------------------------------------------


async def test_approving_twice_executes_once(
    db_session: AsyncSession,
    embedder: EmbeddingProvider,
    redis_client: Redis,
    registry: OAuthRegistry,
) -> None:
    """The decision arrives from a browser, and browsers retry. A double-clicked
    approve must create one event, not two — so the status transition is the
    idempotency key."""
    organization, owner, _ = await make_org_with_owner(db_session)
    await connect_calendar(db_session, redis_client, registry, organization.id)

    _, approval = await service(db_session, embedder).propose_calendar_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None

    await service(db_session, embedder).approve(organization.id, approval.id, owner.id, registry)

    with pytest.raises(ConflictError, match="already approved"):
        await service(db_session, embedder).approve(
            organization.id, approval.id, owner.id, registry
        )

    assert len(FakeCalendarClient.created) == 1


async def test_rejecting_cancels_the_run(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """A run left `paused_for_approval` after a rejection looks resumable forever —
    and `resume_calendar_run` checks exactly that status, so it would be genuinely
    resumable by anyone who found it."""
    organization, owner, _ = await make_org_with_owner(db_session)

    _, approval = await service(db_session, embedder).propose_calendar_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None

    rejected = await service(db_session, embedder).reject(
        organization.id, approval.id, owner.id, "We already have that meeting."
    )

    assert rejected.status is ApprovalStatus.REJECTED
    assert rejected.reason == "We already have that meeting."

    run = await db_session.get(AgentRun, approval.agent_run_id)
    assert run is not None
    assert run.status is RunStatus.CANCELLED
    assert run.checkpoint is None
    assert FakeCalendarClient.created == []

    # Asserted in SQL, not through the ORM, and that is the whole point of this
    # line. Assigning Python `None` to a JSONB column stores the JSON value `null`
    # unless the column sets `none_as_null=True` — and SQLAlchemy reads *both* back
    # as `None`, so the assertion above passed while Postgres held `'null'`.
    #
    # NULL is what "there is nothing to resume" means. An operator asking `WHERE
    # checkpoint IS NOT NULL` for stuck runs would have found every cancelled run
    # in the system. Only looking at the database showed it.
    cleared = await db_session.scalar(
        text("SELECT checkpoint IS NULL FROM agent_runs WHERE id = :id"),
        {"id": approval.agent_run_id},
    )
    assert cleared is True


async def test_a_rejected_request_cannot_then_be_approved(
    db_session: AsyncSession, embedder: EmbeddingProvider, registry: OAuthRegistry
) -> None:
    organization, owner, _ = await make_org_with_owner(db_session)

    _, approval = await service(db_session, embedder).propose_calendar_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None
    await service(db_session, embedder).reject(organization.id, approval.id, owner.id)

    with pytest.raises(ConflictError, match="already rejected"):
        await service(db_session, embedder).approve(
            organization.id, approval.id, owner.id, registry
        )

    assert FakeCalendarClient.created == []


async def test_an_expired_request_cannot_be_approved(
    db_session: AsyncSession, embedder: EmbeddingProvider, registry: OAuthRegistry
) -> None:
    """The action was composed against facts that were true when it was proposed.
    Executing it a week later runs it against facts that may not be."""
    organization, owner, _ = await make_org_with_owner(db_session)

    _, approval = await service(db_session, embedder).propose_calendar_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None
    approval.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()

    with pytest.raises(ConflictError, match="expired"):
        await service(db_session, embedder).approve(
            organization.id, approval.id, owner.id, registry
        )

    assert FakeCalendarClient.created == []


async def test_an_expired_request_is_hidden_from_the_inbox(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Filtered by the clock rather than by `status`, because nothing writes `EXPIRED`
    until a sweep runs. Showing it would invite somebody to click a button that then
    refuses them."""
    organization, owner, _ = await make_org_with_owner(db_session)

    _, approval = await service(db_session, embedder).propose_calendar_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None
    approval.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()

    assert await service(db_session, embedder).list_pending(organization.id) == []


async def test_the_sweep_cancels_runs_nobody_decided_on(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Expiry is a normal outcome, not an error: a run that ends without acting
    because nobody approved it is the system working correctly."""
    organization, owner, _ = await make_org_with_owner(db_session)

    _, approval = await service(db_session, embedder).propose_calendar_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None
    approval.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()

    assert await service(db_session, embedder).expire_overdue(organization.id) == 1

    await db_session.refresh(approval)
    run = await db_session.get(AgentRun, approval.agent_run_id)
    assert approval.status is ApprovalStatus.EXPIRED
    assert run is not None
    assert run.status is RunStatus.CANCELLED


# --------------------------------------------------------------------------
# failure and tenancy
# --------------------------------------------------------------------------


async def test_a_failed_execution_leaves_the_decision_standing(
    db_session: AsyncSession,
    embedder: EmbeddingProvider,
    redis_client: Redis,
    registry: OAuthRegistry,
) -> None:
    """A human did approve it; what failed was the execution.

    The approval moves to `APPROVED` *before* the side effect runs, so a failure
    afterwards is recorded as "authorised, and the attempt failed" rather than looking
    like a decision nobody made.
    """
    organization, owner, _ = await make_org_with_owner(db_session)
    await connect_calendar(db_session, redis_client, registry, organization.id)
    FakeCalendarClient.fail_with = OAuthRevokedError("Google said no")

    _, approval = await service(db_session, embedder).propose_calendar_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None
    # Captured before the call, because the failure path rolls the session back —
    # and a rollback expires every ORM object regardless of `expire_on_commit`.
    # Reading `approval.id` afterwards is a lazy refresh, which under asyncio raises
    # `MissingGreenlet`. The same discipline `AgentService` uses for `run.id`.
    approval_id, run_id = approval.id, approval.agent_run_id

    with pytest.raises(OAuthRevokedError):
        await service(db_session, embedder).approve(
            organization.id, approval_id, owner.id, registry
        )

    stored = await db_session.get(Approval, approval_id)
    run = await db_session.get(AgentRun, run_id)
    assert stored is not None
    assert stored.status is ApprovalStatus.APPROVED
    assert run is not None
    assert run.status is RunStatus.FAILED


async def test_approving_without_a_connected_calendar_fails_the_run(
    db_session: AsyncSession, embedder: EmbeddingProvider, registry: OAuthRegistry
) -> None:
    """Proposing needs no integration; executing does. The failure lands at the moment
    somebody tries to act, with a message telling them to connect."""
    organization, owner, _ = await make_org_with_owner(db_session)

    _, approval = await service(db_session, embedder).propose_calendar_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None

    with pytest.raises(NotFoundError, match="No active google_calendar"):
        await service(db_session, embedder).approve(
            organization.id, approval.id, owner.id, registry
        )


async def test_another_tenants_approval_is_not_found(
    db_session: AsyncSession, embedder: EmbeddingProvider, registry: OAuthRegistry
) -> None:
    """404 rather than 403 — a distinct status would confirm that a specific approval
    id exists in *some* organization."""
    organization, owner, _ = await make_org_with_owner(db_session)
    other, other_owner, _ = await make_org_with_owner(db_session)

    _, approval = await service(db_session, embedder).propose_calendar_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None

    with pytest.raises(NotFoundError):
        await service(db_session, embedder).approve(other.id, approval.id, other_owner.id, registry)

    assert FakeCalendarClient.created == []


async def test_the_inbox_is_scoped_to_the_tenant(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    organization, owner, _ = await make_org_with_owner(db_session)
    other, _, _ = await make_org_with_owner(db_session)

    await service(db_session, embedder).propose_calendar_action(
        organization.id, owner.id, INSTRUCTION
    )

    assert len(await service(db_session, embedder).list_pending(organization.id)) == 1
    assert await service(db_session, embedder).list_pending(other.id) == []


async def test_one_approval_per_run(db_session: AsyncSession, embedder: EmbeddingProvider) -> None:
    """The unique index says so out loud. M12 gates a single action per run, and a
    second row would be possible-but-unhandled rather than merely unused."""
    organization, owner, _ = await make_org_with_owner(db_session)

    _, approval = await service(db_session, embedder).propose_calendar_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None

    rows = await db_session.scalars(
        select(Approval).where(Approval.agent_run_id == approval.agent_run_id)
    )
    assert len(list(rows)) == 1
