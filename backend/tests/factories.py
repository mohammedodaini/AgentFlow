"""Test data builders.

The problem factories solve is not typing. It is *reading*. A test that opens
with eight lines constructing a user, an organization and a membership buries
its one interesting line — and when a required column is added, every one of
those tests has to be edited.

The rule here: a factory fills in everything the test does not care about, and
the test names only what it does. A test about role rules should read

    organization, user, membership = await make_org_with_owner(session)

and a test about email uniqueness should read

    await make_user(session, email="ada@example.com")

so that the argument which *is* named is visibly the subject of the test.

Randomised, not fixed. Two calls to `make_user` produce different emails, so a
test can create ten users without inventing ten addresses — and a test that
accidentally depends on a specific value fails immediately rather than on the
day someone adds an eleventh.
"""

from __future__ import annotations

from typing import Any

from faker import Faker
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models import Membership, Organization, Role, User
from app.services.organization_service import slugify

fake = Faker()
Faker.seed(0)
"""Seeded so a failure is reproducible from the same test order.

Not reseeded *per test*: that would hand every test the same email and defeat
the uniqueness the randomness exists to provide.
"""

DEFAULT_PASSWORD = "correct horse battery staple"  # noqa: S105 — test fixture, not a credential
"""Long enough to satisfy the M3 policy, and the same everywhere so a test that
logs in does not have to remember what it registered with."""

_SHARED_PASSWORD_HASH = hash_password(DEFAULT_PASSWORD)
"""Hashed exactly once for the whole session.

Argon2id is deliberately ~50 ms per call — that is the entire point of it.
Hashing per created user would put a minute of pure CPU into a suite that
otherwise runs in nine seconds. Tests that actually exercise hashing pass their
own password; everything else shares this.
"""


async def make_user(
    session: AsyncSession,
    *,
    email: str | None = None,
    password: str | None = None,
    **fields: Any,
) -> User:
    """Create a persisted user.

    Pass `password` only when the test logs in with a specific one; otherwise
    the shared pre-computed hash is used, and the user still has a valid,
    verifiable password of `DEFAULT_PASSWORD`.
    """
    user = User(
        email=email or fake.unique.email(),
        password_hash=hash_password(password) if password else _SHARED_PASSWORD_HASH,
        full_name=fields.pop("full_name", fake.name()),
        **fields,
    )
    session.add(user)
    await session.flush()
    return user


async def make_organization(
    session: AsyncSession, *, name: str | None = None, slug: str | None = None, **fields: Any
) -> Organization:
    """Create a persisted organization with a unique slug."""
    name = name or fake.unique.company()
    organization = Organization(name=name, slug=slug or slugify(name), **fields)
    session.add(organization)
    await session.flush()
    return organization


async def make_membership(
    session: AsyncSession,
    *,
    user: User,
    organization: Organization,
    role: Role = Role.MEMBER,
) -> Membership:
    """Put a user in an organization with a role."""
    membership = Membership(user_id=user.id, organization_id=organization.id, role=role)
    session.add(membership)
    await session.flush()
    return membership


async def make_org_with_owner(
    session: AsyncSession, *, role: Role = Role.OWNER
) -> tuple[Organization, User, Membership]:
    """The most common setup: an organization and somebody who can act in it.

    Returns all three because tests need different pieces — the org id for a
    header, the user for a second membership, the membership as the `actor`
    argument to a service call.
    """
    organization = await make_organization(session)
    user = await make_user(session)
    membership = await make_membership(session, user=user, organization=organization, role=role)
    return organization, user, membership
