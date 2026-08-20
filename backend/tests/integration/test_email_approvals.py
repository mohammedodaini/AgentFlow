"""The email agent through the approval loop, against real Postgres (M14).

M12 built the approval machinery around the calendar and wrote, in ADR-0015:

    "The approval machinery is provider-agnostic; adding the second action kind is
     a `requested_action['kind']` and an executor."

This file is the test of that claim. Nothing in `ApprovalService` changed except a
second propose method, and nothing in `AgentService` changed except dispatching on
the stored kind instead of calling one method by name.

The email agent is also the first **irreversible** side effect in the system. A
calendar event can be deleted; a sent email cannot be recalled — which is why the
sharpest tests here are the ones asserting how many times it happened, and that
proposing never reaches Gmail at all.

Gmail is stubbed by replacing the client *in one module*. Patching `httpx` globally
would silence every HTTP call in the process, including ones a test never meant to
stub.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.email import tools as email_tools
from app.core.config import get_settings
from app.core.exceptions import ConflictError
from app.integrations import OAuthRegistry
from app.integrations.base import OAuthRevokedError
from app.integrations.gmail.client import EmailDraft
from app.integrations.offline import OfflineOAuthProvider
from app.llm.offline import OfflineLLM
from app.models.agent_run import RunStatus
from app.models.approval import ApprovalStatus
from app.models.integration import Provider
from app.rag.embeddings import EmbeddingProvider
from app.services.approval_service import ApprovalService
from app.services.integration_service import IntegrationService
from tests.factories import make_org_with_owner
from tests.unit.test_oauth import code_from

INSTRUCTION = "Email ada@example.test about the Q3 numbers saying the report is ready."
GMAIL = Provider.GMAIL


class FakeGmailClient:
    """Records what it drafted and sent instead of calling Gmail.

    Class-scoped counters, because the sharpest question about an irreversible
    action is *how many times* it happened — and a per-instance counter cannot
    answer it when the code under test constructs its own client.
    """

    drafted: list[dict[str, Any]] = []
    sent: list[str] = []
    fail_send_with: Exception | None = None

    @classmethod
    def reset(cls) -> None:
        cls.drafted = []
        cls.sent = []
        cls.fail_send_with = None

    async def create_draft(
        self, access_token: str, *, to: str, subject: str, body: str
    ) -> EmailDraft:
        del access_token
        FakeGmailClient.drafted.append({"to": to, "subject": subject, "body": body})
        index = len(FakeGmailClient.drafted)
        return EmailDraft(
            draft_id=f"draft-{index}",
            message_id=f"msg-{index}",
            to=to,
            subject=subject,
            body=body,
        )

    async def send_draft(self, access_token: str, *, draft_id: str) -> str:
        del access_token

        if FakeGmailClient.fail_send_with is not None:
            raise FakeGmailClient.fail_send_with

        FakeGmailClient.sent.append(draft_id)
        return f"sent-{draft_id}"


@pytest.fixture(autouse=True)
def _stub_gmail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the Gmail client inside the tools module, and nowhere else."""
    FakeGmailClient.reset()
    monkeypatch.setattr(email_tools, "GmailClient", FakeGmailClient)


@pytest.fixture
def registry() -> OAuthRegistry:
    return OAuthRegistry({GMAIL: OfflineOAuthProvider(GMAIL.value, ["gmail.compose"])})


def service(db_session: AsyncSession, embedder: EmbeddingProvider) -> ApprovalService:
    """A *fresh* service over the same session, per call — the restart test needs
    two instances that share nothing but the database."""
    return ApprovalService(db_session, embedder, OfflineLLM(), get_settings())


async def connect_gmail(
    db_session: AsyncSession, redis_client: Redis, registry: OAuthRegistry, org: uuid.UUID
) -> None:
    """Give the organization a working credential, through the real OAuth flow."""
    integrations = IntegrationService(db_session, redis_client, registry, get_settings())
    pending = await integrations.begin_connect(org, None, GMAIL)
    await integrations.complete_callback(
        GMAIL, state=pending.state, code=code_from(pending.authorize_url)
    )


# --------------------------------------------------------------------------
# proposing
# --------------------------------------------------------------------------


async def test_proposing_pauses_the_run_and_sends_nothing(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """M12's deferred half, working: the agent asks, and no mail moves.

    Note there is no Gmail connection in this test at all. Proposing cannot reach a
    provider, so it cannot need one — which is the structural guarantee rather than
    a promise that the code is careful.
    """
    organization, owner, _ = await make_org_with_owner(db_session)

    run, approval = await service(db_session, embedder).propose_email_action(
        organization.id, owner.id, INSTRUCTION
    )

    assert run.status is RunStatus.PAUSED_FOR_APPROVAL
    assert approval is not None
    assert approval.status is ApprovalStatus.PENDING
    assert FakeGmailClient.drafted == []
    assert FakeGmailClient.sent == []


async def test_the_whole_message_is_stored_for_the_human_to_read(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The body is the thing that gets sent, so the body is what the person
    deciding has to see. A summary would be a second account of it."""
    organization, owner, _ = await make_org_with_owner(db_session)

    _, approval = await service(db_session, embedder).propose_email_action(
        organization.id, owner.id, INSTRUCTION
    )

    assert approval is not None
    assert approval.requested_action["to"] == "ada@example.test"
    assert approval.requested_action["body"] == "the report is ready."
    assert approval.summary == "Send an email to ada@example.test with the subject 'the Q3 numbers'"


async def test_the_body_is_not_copied_into_the_trace(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """**The deliberate exception to M12's rule, and its honest limit.**

    Everywhere else the whole action goes into `agent_steps`, which is operational
    data — read while debugging, exported into whatever observability stack a
    deployment runs. `approvals` is tenant-scoped and returned only through checked
    endpoints, so the body of somebody's email belongs there.

    The second assertion is the honest half: the body **is** in `agent_runs.input`,
    because the user typed it. This does not hide it, it stops it being copied. The
    first draft of the graph redacted the body from the `propose` step while the
    `compose` step recorded the whole instruction containing it, and this test is
    what found that.
    """
    organization, owner, _ = await make_org_with_owner(db_session)

    run, _ = await service(db_session, embedder).propose_email_action(
        organization.id, owner.id, INSTRUCTION
    )

    recorded = str([step.tool_input for step in run.steps])
    assert "the report is ready" not in recorded
    assert "ada@example.test" in recorded, "the recipient is operational, and stays"
    assert "the report is ready" in str(run.input), "one unavoidable copy, and only one"


async def test_an_unparseable_instruction_asks_nobody_for_anything(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    organization, owner, _ = await make_org_with_owner(db_session)

    run, approval = await service(db_session, embedder).propose_email_action(
        organization.id, owner.id, "Drop the team a line about the release"
    )

    assert approval is None
    assert run.status is RunStatus.SUCCEEDED
    assert run.checkpoint is None
    assert "alice@example.com" in str(run.output)


# --------------------------------------------------------------------------
# approving
# --------------------------------------------------------------------------


async def test_approving_drafts_and_sends_exactly_once(
    db_session: AsyncSession,
    redis_client: Redis,
    embedder: EmbeddingProvider,
    registry: OAuthRegistry,
) -> None:
    """The loop end to end, and the count is the assertion that matters."""
    organization, owner, _ = await make_org_with_owner(db_session)
    await connect_gmail(db_session, redis_client, registry, organization.id)

    _, approval = await service(db_session, embedder).propose_email_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None

    decided = await service(db_session, embedder).approve(
        organization.id, approval.id, owner.id, registry
    )

    assert decided.status is ApprovalStatus.APPROVED
    assert FakeGmailClient.drafted == [
        {
            "to": "ada@example.test",
            "subject": "the Q3 numbers",
            "body": "the report is ready.",
        }
    ]
    assert FakeGmailClient.sent == ["draft-1"]


async def test_what_was_approved_is_what_was_sent(
    db_session: AsyncSession,
    redis_client: Redis,
    embedder: EmbeddingProvider,
    registry: OAuthRegistry,
) -> None:
    """ADR-0015's invariant, at the one place it could now break.

    M14 put a registry lookup between the approval and the executor, so "identical
    by construction" became "identical, and checked".
    """
    organization, owner, _ = await make_org_with_owner(db_session)
    await connect_gmail(db_session, redis_client, registry, organization.id)

    _, approval = await service(db_session, embedder).propose_email_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None
    stored = dict(approval.requested_action)

    await service(db_session, embedder).approve(organization.id, approval.id, owner.id, registry)

    assert FakeGmailClient.drafted[0]["body"] == stored["body"]


async def test_a_tampered_checkpoint_stops_the_send(
    db_session: AsyncSession,
    redis_client: Redis,
    embedder: EmbeddingProvider,
    registry: OAuthRegistry,
) -> None:
    """**The guard that cannot fire today, and is worth having anyway.**

    The approval row and the run's checkpoint are written in one transaction from
    the same dict, so they cannot disagree. If they ever did, a human would have
    authorised one message and another would have gone out — the single failure
    this whole design exists to prevent, and not one to learn about from a support
    ticket.
    """
    organization, owner, _ = await make_org_with_owner(db_session)
    await connect_gmail(db_session, redis_client, registry, organization.id)

    run, approval = await service(db_session, embedder).propose_email_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None

    tampered = dict(run.checkpoint or {})
    tampered["proposed_action"] = dict(tampered["proposed_action"]) | {"to": "mallory@evil.test"}
    run.checkpoint = tampered
    await db_session.flush()

    with pytest.raises(ConflictError):
        await service(db_session, embedder).approve(
            organization.id, approval.id, owner.id, registry
        )

    assert FakeGmailClient.sent == []


async def test_rejecting_sends_nothing_and_cancels_the_run(
    db_session: AsyncSession, embedder: EmbeddingProvider, registry: OAuthRegistry
) -> None:
    organization, owner, _ = await make_org_with_owner(db_session)

    run, approval = await service(db_session, embedder).propose_email_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None

    decided = await service(db_session, embedder).reject(
        organization.id, approval.id, owner.id, reason="Wrong person"
    )

    assert decided.status is ApprovalStatus.REJECTED
    assert FakeGmailClient.sent == []

    cancelled = await service(db_session, embedder)._agents.get_run(organization.id, run.id)  # noqa: SLF001
    assert cancelled.status is RunStatus.CANCELLED


async def test_approving_twice_is_refused(
    db_session: AsyncSession,
    redis_client: Redis,
    embedder: EmbeddingProvider,
    registry: OAuthRegistry,
) -> None:
    """Browsers retry, and people double-click. The status transition is the
    idempotency key — and here the cost of getting it wrong is two identical emails
    in somebody's inbox."""
    organization, owner, _ = await make_org_with_owner(db_session)
    await connect_gmail(db_session, redis_client, registry, organization.id)

    _, approval = await service(db_session, embedder).propose_email_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None

    await service(db_session, embedder).approve(organization.id, approval.id, owner.id, registry)

    with pytest.raises(ConflictError):
        await service(db_session, embedder).approve(
            organization.id, approval.id, owner.id, registry
        )

    assert FakeGmailClient.sent == ["draft-1"]


async def test_a_send_failure_leaves_the_decision_standing(
    db_session: AsyncSession,
    redis_client: Redis,
    embedder: EmbeddingProvider,
    registry: OAuthRegistry,
) -> None:
    """M12's bug 3, at the second action kind: the run fails, and the record that a
    human authorised it survives. Marking it approved only on success would make a
    failed send indistinguishable from a decision nobody made."""
    organization, owner, _ = await make_org_with_owner(db_session)
    await connect_gmail(db_session, redis_client, registry, organization.id)

    # Read the ids out *before* the failing call. `AgentService._finish_failed`
    # rolls the session back — it must, because the session may be unusable — and a
    # rollback expires every ORM object regardless of `expire_on_commit`. Touching
    # `organization.id` afterwards is a lazy load under asyncio, which raises
    # `MissingGreenlet` from inside the test rather than the code under test. The
    # seventh time this project has paid for that fact.
    organization_id = organization.id
    owner_id = owner.id

    _, approval = await service(db_session, embedder).propose_email_action(
        organization_id, owner_id, INSTRUCTION
    )
    assert approval is not None
    approval_id = approval.id
    FakeGmailClient.fail_send_with = OAuthRevokedError("Gmail rejected the credential.")

    with pytest.raises(Exception, match="Gmail|revoked|Reconnect"):
        await service(db_session, embedder).approve(
            organization_id, approval_id, owner_id, registry
        )

    still = await service(db_session, embedder).get(organization_id, approval_id)
    assert still.status is ApprovalStatus.APPROVED


async def test_approving_without_a_connection_fails_and_says_so(
    db_session: AsyncSession, embedder: EmbeddingProvider, registry: OAuthRegistry
) -> None:
    """No Gmail connected. The error names the action the user can take."""
    organization, owner, _ = await make_org_with_owner(db_session)

    _, approval = await service(db_session, embedder).propose_email_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None

    with pytest.raises(Exception, match="gmail"):
        await service(db_session, embedder).approve(
            organization.id, approval.id, owner.id, registry
        )

    assert FakeGmailClient.sent == []


async def test_a_paused_email_run_survives_a_restart(
    db_session: AsyncSession,
    redis_client: Redis,
    embedder: EmbeddingProvider,
    registry: OAuthRegistry,
) -> None:
    """The claim M12 made, holding for the second action kind.

    Proposed with one service instance, resumed with a completely fresh one that
    shares nothing but the database — which is the only thing a real restart
    preserves.
    """
    organization, owner, _ = await make_org_with_owner(db_session)
    await connect_gmail(db_session, redis_client, registry, organization.id)

    _, approval = await service(db_session, embedder).propose_email_action(
        organization.id, owner.id, INSTRUCTION
    )
    assert approval is not None
    approval_id = approval.id

    # A different instance, as a different process would be.
    decided = await service(db_session, embedder).approve(
        organization.id, approval_id, owner.id, registry
    )

    assert decided.status is ApprovalStatus.APPROVED
    assert FakeGmailClient.sent == ["draft-1"]
