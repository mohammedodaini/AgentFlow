"""The audit trail against real Postgres (M16).

Two properties carry the whole feature, and each has a test that would fail
loudly without it:

- a **failed** sign-in is recorded, even though the request raises;
- a payload **never** carries a credential, however it was nested.

The rest guard the shape: the event commits with the thing it describes, the
actor is who *did* it rather than who it was done to, and reads are tenant-scoped.
"""

from __future__ import annotations

import uuid

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import AuthService
from app.auth.tokens import TokenService
from app.core.exceptions import AuthenticationError
from app.models.event import Event, EventType
from app.models.membership import Role
from app.schemas.auth import LoginRequest, RegisterRequest
from app.services.event_service import EventService
from app.services.organization_service import OrganizationService
from tests.factories import make_org_with_owner, make_user

PASSWORD = "correct-horse-battery-staple"


def auth(db_session: AsyncSession, redis_client: Redis) -> AuthService:
    return AuthService(db_session, TokenService(redis_client), ip_address="203.0.113.7")


async def events_of(db_session: AsyncSession, event_type: EventType) -> list[Event]:
    statement = select(Event).where(Event.event_type == event_type.value)
    return list((await db_session.scalars(statement)).all())


# --- the two that carry the feature --------------------------------------


async def test_a_failed_sign_in_is_recorded(db_session: AsyncSession, redis_client: Redis) -> None:
    """**The most valuable row in the table, and the one easiest to lose.**

    This path raises, and `get_db` rolls back on any exception — so an event that
    merely *flushed* would be discarded by the failure it documents. A trail with
    every successful sign-in and no failed ones is worse than useless, because it
    looks complete: credential stuffing is invisible in it.
    """
    with pytest.raises(AuthenticationError):
        await auth(db_session, redis_client).login(
            LoginRequest(email="nobody@agentflow.dev", password=PASSWORD)
        )

    recorded = await events_of(db_session, EventType.USER_SIGN_IN_FAILED)

    assert len(recorded) == 1
    assert recorded[0].payload["reason"] == "unknown_email"
    assert recorded[0].ip_address == "203.0.113.7"
    # No user id: the whole point is that the credential resolved to nobody. The
    # *email* is kept, because "which addresses is somebody guessing?" is the
    # question an investigation actually asks.
    assert recorded[0].actor_user_id is None
    assert recorded[0].payload["email"] == "nobody@agentflow.dev"


async def test_a_wrong_password_names_the_user(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """A real account with a bad guess — the shape that matters for detecting an
    attack on one person rather than a spray across many."""
    user = await make_user(db_session, email="ada@agentflow.dev", password=PASSWORD)

    with pytest.raises(AuthenticationError):
        await auth(db_session, redis_client).login(
            LoginRequest(email="ada@agentflow.dev", password="not-the-password")
        )

    recorded = await events_of(db_session, EventType.USER_SIGN_IN_FAILED)

    assert recorded[0].actor_user_id == user.id
    assert recorded[0].payload["reason"] == "bad_password"


async def test_a_payload_never_carries_a_credential(db_session: AsyncSession) -> None:
    """**The rule the whole table depends on.**

    An audit trail is read by people never authorised to read the *data* —
    auditors, support, whoever is handling an incident — and its value comes from
    being widely readable. A payload carrying a token turns the one table
    everybody can see into the one place everything leaks.

    Nested, because that is how a credential arrives without anybody meaning it
    to: a top-level check passes and the purpose fails.
    """
    organization, owner, _ = await make_org_with_owner(db_session)

    await EventService(db_session).record(
        EventType.INTEGRATION_CONNECTED,
        organization_id=organization.id,
        actor_user_id=owner.id,
        provider="slack",
        grant={"access_token": "xoxb-very-secret", "scope": "channels:read"},
        password="hunter2",
        body="the full text of somebody's email",
    )

    recorded = (await events_of(db_session, EventType.INTEGRATION_CONNECTED))[0]

    assert recorded.payload["provider"] == "slack"
    assert recorded.payload["password"] == "[redacted]"
    assert recorded.payload["body"] == "[redacted]"
    assert recorded.payload["grant"]["access_token"] == "[redacted]"
    # What is *not* sensitive survives, or the redaction would make the trail
    # useless rather than safe.
    assert recorded.payload["grant"]["scope"] == "channels:read"


# --- shape ---------------------------------------------------------------


async def test_registering_records_who_and_where(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    await auth(db_session, redis_client).register(
        RegisterRequest(email="grace@agentflow.dev", password=PASSWORD, full_name="Grace")
    )

    recorded = (await events_of(db_session, EventType.USER_REGISTERED))[0]

    assert recorded.actor_user_id is not None
    assert recorded.organization_id is not None
    assert recorded.ip_address == "203.0.113.7"


async def test_the_actor_is_who_did_it_not_who_it_was_done_to(
    db_session: AsyncSession,
) -> None:
    """Storing the target in `actor_user_id` would make "what did this person do?"
    return everything done *to* them — the opposite question, silently."""
    organization, owner, owner_membership = await make_org_with_owner(db_session)
    # `invite_member` adds an *existing* account (M3 does not mail strangers), so
    # the invitee has to exist first.
    await make_user(db_session, email="new@agentflow.dev")

    _, invited = await OrganizationService(db_session).invite_member(
        organization_id=organization.id,
        email="new@agentflow.dev",
        role=Role.MEMBER,
        actor=owner_membership,
    )

    recorded = (await events_of(db_session, EventType.MEMBER_INVITED))[0]

    assert recorded.actor_user_id == owner.id
    assert recorded.payload["target_user_id"] == str(invited.id)


async def test_a_uuid_payload_value_does_not_break_the_write(
    db_session: AsyncSession,
) -> None:
    """JSONB cannot hold a UUID object and `json.dumps` raises on one — which
    would take down a request through a path that exists to be harmless."""
    organization, owner, _ = await make_org_with_owner(db_session)

    await EventService(db_session).record(
        EventType.DOCUMENT_UPLOADED,
        organization_id=organization.id,
        actor_user_id=owner.id,
        document_id=organization.id,
    )

    recorded = (await events_of(db_session, EventType.DOCUMENT_UPLOADED))[0]

    assert recorded.payload["document_id"] == str(organization.id)


async def test_a_long_value_is_truncated(db_session: AsyncSession) -> None:
    """Payload values are facts, not documents. A 40-page extract in an audit row
    is somebody having passed the wrong variable."""
    organization, owner, _ = await make_org_with_owner(db_session)

    await EventService(db_session).record(
        EventType.DOCUMENT_UPLOADED,
        organization_id=organization.id,
        actor_user_id=owner.id,
        title="x" * 2000,
    )

    recorded = (await events_of(db_session, EventType.DOCUMENT_UPLOADED))[0]

    assert len(recorded.payload["title"]) < 600


async def test_recording_never_breaks_the_operation(db_session: AsyncSession) -> None:
    """The one place in this codebase where swallowing an exception is the
    considered choice: an audit write that raised would turn a working feature
    into an outage."""
    service = EventService(db_session)

    # An organization id that does not exist violates the foreign key.
    await service.record(EventType.DOCUMENT_UPLOADED, organization_id=uuid.uuid4())

    # The point is that control reached here at all.
    assert True


async def test_the_trail_is_tenant_scoped(db_session: AsyncSession) -> None:
    """An audit log readable across tenants would name every customer, their
    staff, and their habits."""
    mine, my_owner, _ = await make_org_with_owner(db_session)
    theirs, their_owner, _ = await make_org_with_owner(db_session)
    service = EventService(db_session)

    await service.record(
        EventType.DOCUMENT_UPLOADED, organization_id=mine.id, actor_user_id=my_owner.id
    )
    await service.record(
        EventType.DOCUMENT_UPLOADED, organization_id=theirs.id, actor_user_id=their_owner.id
    )

    listed = await service.list_for_organization(mine.id)

    assert len(listed) == 1
    assert listed[0].organization_id == mine.id


async def test_the_timestamp_comes_from_the_database(db_session: AsyncSession) -> None:
    """Evidence should not be timestamped by the thing being audited: a server
    with a skewed — or compromised — clock would otherwise write history in
    whatever order it liked."""
    organization, owner, _ = await make_org_with_owner(db_session)

    await EventService(db_session).record(
        EventType.DOCUMENT_UPLOADED, organization_id=organization.id, actor_user_id=owner.id
    )

    default = await db_session.scalar(
        text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'events' AND column_name = 'created_at'"
        )
    )

    assert default is not None and "now()" in default


async def test_the_table_has_no_updated_at(db_session: AsyncSession) -> None:
    """Append-only, and the schema says so. A column recording when a row changed
    would imply these rows change, and an audit entry that can be edited is not
    evidence."""
    columns = list(
        await db_session.scalars(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = 'events'")
        )
    )

    assert "updated_at" not in columns
    assert "created_at" in columns
