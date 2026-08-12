"""The schema as PostgreSQL actually enforces it (M2).

tests/unit/test_models.py asserts what the *metadata* says. This file asserts
what the *server* does — and those are different claims. A constraint declared
in Python but never migrated exists in exactly one of the two places.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Membership, Organization, Role, User


async def _make_org(session: AsyncSession, slug: str = "acme") -> Organization:
    organization = Organization(name="Acme Corp", slug=slug)
    session.add(organization)
    await session.flush()
    return organization


async def _make_user(session: AsyncSession, email: str = "ada@example.com") -> User:
    user = User(email=email, password_hash="not-a-real-hash")
    session.add(user)
    await session.flush()
    return user


async def test_insert_and_read_back_the_tenancy_graph(db_session: AsyncSession) -> None:
    """The core join: a person, a company, and the role connecting them."""
    organization = await _make_org(db_session)
    user = await _make_user(db_session)
    db_session.add(Membership(user_id=user.id, organization_id=organization.id, role=Role.OWNER))
    await db_session.commit()

    found = await db_session.scalar(
        select(Membership).where(Membership.organization_id == organization.id)
    )

    assert found is not None
    assert found.user_id == user.id
    assert found.role is Role.OWNER


async def test_ids_are_uuid7_and_assigned_at_flush(db_session: AsyncSession) -> None:
    """Client-side generation: the id is ours at flush, before any commit.

    Pinning the exact moment matters. `default=` is a *column* default, so it
    runs while SQLAlchemy builds the INSERT — not when you call the
    constructor. Code that logs `obj.id` right after `Organization(...)` gets
    None, and this test is the reason nobody has to discover that twice.
    """
    organization = Organization(name="Acme Corp", slug="acme")
    assert organization.id is None

    db_session.add(organization)
    await db_session.flush()

    assert isinstance(organization.id, uuid.UUID)
    assert organization.id.version == 7


async def test_timestamps_are_set_by_the_database(db_session: AsyncSession) -> None:
    """`server_default=now()` — the application never supplies these values."""
    organization = await _make_org(db_session)
    await db_session.commit()
    await db_session.refresh(organization)

    assert organization.created_at.tzinfo is not None, "must be timezone-aware (timestamptz)"
    assert abs((datetime.now(UTC) - organization.created_at).total_seconds()) < 60


async def test_duplicate_email_is_rejected(db_session: AsyncSession) -> None:
    """Two accounts sharing one email is the bug that breaks password reset."""
    await _make_user(db_session, email="ada@example.com")
    await db_session.commit()

    db_session.add(User(email="ada@example.com", password_hash="x"))

    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_a_user_cannot_join_the_same_organization_twice(db_session: AsyncSession) -> None:
    """The constraint no amount of application code can replace."""
    organization = await _make_org(db_session)
    user = await _make_user(db_session)
    db_session.add(Membership(user_id=user.id, organization_id=organization.id))
    await db_session.commit()

    db_session.add(Membership(user_id=user.id, organization_id=organization.id, role=Role.ADMIN))

    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_a_user_can_belong_to_several_organizations(db_session: AsyncSession) -> None:
    """The entire reason memberships is a table — the consultant case."""
    first = await _make_org(db_session, slug="acme")
    second = await _make_org(db_session, slug="globex")
    user = await _make_user(db_session)
    db_session.add_all(
        [
            Membership(user_id=user.id, organization_id=first.id, role=Role.OWNER),
            Membership(user_id=user.id, organization_id=second.id, role=Role.MEMBER),
        ]
    )
    await db_session.commit()

    memberships = (
        await db_session.scalars(select(Membership).where(Membership.user_id == user.id))
    ).all()

    assert {membership.organization_id for membership in memberships} == {first.id, second.id}


async def test_deleting_an_organization_cascades_to_memberships(db_session: AsyncSession) -> None:
    """ON DELETE CASCADE, enforced by the server rather than by the ORM."""
    organization = await _make_org(db_session)
    user = await _make_user(db_session)
    db_session.add(Membership(user_id=user.id, organization_id=organization.id))
    await db_session.commit()

    await db_session.delete(organization)
    await db_session.commit()

    assert await db_session.scalar(select(Membership)) is None
    assert await db_session.scalar(select(User)) is not None, "the person still exists"


async def test_role_defaults_to_member(db_session: AsyncSession) -> None:
    """An invite that forgets to name a role must not grant ownership."""
    organization = await _make_org(db_session)
    user = await _make_user(db_session)
    membership = Membership(user_id=user.id, organization_id=organization.id)
    db_session.add(membership)
    await db_session.commit()
    await db_session.refresh(membership)

    assert membership.role is Role.MEMBER


async def test_role_is_stored_as_its_lowercase_value(db_session: AsyncSession) -> None:
    """Guards `values_callable`.

    Without it SQLAlchemy persists the member *name* — "OWNER" — while the API
    and the frontend both speak "owner". The mismatch only surfaces when
    someone queries the database directly, which is always the worst moment.
    """
    organization = await _make_org(db_session)
    user = await _make_user(db_session)
    db_session.add(Membership(user_id=user.id, organization_id=organization.id, role=Role.OWNER))
    await db_session.commit()

    stored = await db_session.scalar(select(Membership.role).select_from(Membership))

    assert str(stored) == "owner"
