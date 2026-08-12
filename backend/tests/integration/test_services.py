"""Service-layer paths the end-to-end suite cannot reach (M4).

Every test here covers a branch a well-behaved HTTP client never exercises: a
user deleted between issuing a token and using it, a password hashed with
parameters from two years ago, a slug space that has run out. They are the
branches most likely to be wrong, precisely because nothing routine runs them.

Written against the service layer directly rather than over HTTP, because that
is the cheapest place to set up a state the API has no endpoint for.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from argon2 import PasswordHasher
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.service import AuthService
from app.auth.tokens import TokenService
from app.core.exceptions import AuthenticationError, ConflictError, NotFoundError
from app.core.security import create_token, needs_rehash, verify_password
from app.models import Role
from app.schemas.auth import LoginRequest
from app.schemas.user import UserUpdate
from app.services import organization_service as organization_service_module
from app.services.organization_service import OrganizationService
from app.services.user_service import UserService
from tests.factories import DEFAULT_PASSWORD, make_org_with_owner, make_organization, make_user

FIVE_MINUTES = timedelta(minutes=5)

_weak_hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
"""Produces a *valid* Argon2 hash with obsolete cost parameters.

Argon2 stores its parameters inside the hash, so the default hasher can still
verify this one — which is exactly the situation `needs_rehash` exists for, and
the only way to test the upgrade path honestly.
"""


class _StubRedis:
    """Enough of Redis for the token service; no denylist behaviour needed here."""

    async def exists(self, *_keys: str) -> int:
        return 0

    async def set(self, *_args: object, **_kwargs: object) -> None:
        return None


def _auth_service(session: AsyncSession) -> AuthService:
    return AuthService(session, TokenService(_StubRedis()))  # type: ignore[arg-type]


class _Credentials:
    """The attribute `get_current_user` reads off HTTPBearer's result."""

    def __init__(self, token: str) -> None:
        self.scheme = "Bearer"
        self.credentials = token


# --------------------------------------------------------------------------
# UserService
# --------------------------------------------------------------------------


async def test_get_profile_raises_for_an_unknown_id(db_session: AsyncSession) -> None:
    """A valid, unexpired token whose user has since been deleted.

    The signature verifies, so nothing upstream catches it — this branch is the
    only thing between that request and an AttributeError on None.
    """
    with pytest.raises(NotFoundError):
        await UserService(db_session).get_profile(uuid.uuid4())


async def test_update_profile_ignores_fields_the_client_did_not_send(
    db_session: AsyncSession,
) -> None:
    """`exclude_unset=True` is what makes PATCH a patch.

    Without it, an omitted optional field arrives as None and blanks the
    column — so changing one field would silently erase the others.
    """
    user = await make_user(db_session, full_name="Ada Lovelace")

    updated = await UserService(db_session).update_profile(user.id, UserUpdate())

    assert updated.full_name == "Ada Lovelace"


# --------------------------------------------------------------------------
# OrganizationService
# --------------------------------------------------------------------------


async def test_slug_allocation_gives_up_rather_than_looping_forever(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound on the dedupe loop, exercised with a tiny limit.

    An unbounded `while True` around a database query is how a request hangs
    instead of failing. Patching the constant proves the guard exists without
    inserting a hundred organizations to reach it.
    """
    monkeypatch.setattr(organization_service_module, "MAX_SLUG_ATTEMPTS", 2)
    owner = await make_user(db_session)
    await make_organization(db_session, name="Acme", slug="acme")
    await make_organization(db_session, name="Acme Two", slug="acme-2")

    with pytest.raises(ConflictError):
        await OrganizationService(db_session).create(name="Acme", owner=owner)


async def test_a_member_cannot_be_added_to_an_organization_twice(
    db_session: AsyncSession,
) -> None:
    """Checked in the service so the API answers 409 rather than surfacing a
    raw IntegrityError from the UNIQUE constraint underneath."""
    organization, _owner, actor = await make_org_with_owner(db_session)
    joiner = await make_user(db_session)
    service = OrganizationService(db_session)

    await service.invite_member(
        organization_id=organization.id, email=joiner.email, role=Role.MEMBER, actor=actor
    )

    with pytest.raises(ConflictError):
        await service.invite_member(
            organization_id=organization.id, email=joiner.email, role=Role.MEMBER, actor=actor
        )


async def test_removing_yourself_needs_no_manager_role(db_session: AsyncSession) -> None:
    """Leaving is not a privileged action — the asymmetry inside `remove_member`."""
    organization, _owner, owner_membership = await make_org_with_owner(db_session)
    leaver = await make_user(db_session)
    service = OrganizationService(db_session)

    membership, _user = await service.invite_member(
        organization_id=organization.id,
        email=leaver.email,
        role=Role.MEMBER,
        actor=owner_membership,
    )

    await service.remove_member(
        organization_id=organization.id,
        target_user_id=membership.user_id,
        actor=membership,
    )

    assert len(await service.list_members(organization.id)) == 1


# --------------------------------------------------------------------------
# AuthService
# --------------------------------------------------------------------------


async def test_a_deactivated_account_cannot_log_in(db_session: AsyncSession) -> None:
    """And it fails with the *same* error as a wrong password.

    Saying "your account is suspended" would tell anyone working through a
    stolen password list which of their guesses were correct.
    """
    user = await make_user(db_session, password=DEFAULT_PASSWORD, is_active=False)

    with pytest.raises(AuthenticationError) as error:
        await _auth_service(db_session).login(
            LoginRequest(email=user.email, password=DEFAULT_PASSWORD)
        )

    assert str(error.value) == "Incorrect email or password"


async def test_login_silently_upgrades_a_weak_password_hash(db_session: AsyncSession) -> None:
    """Cost parameters must rise as hardware gets faster.

    Login is the only moment the plaintext exists, so it is the only moment a
    stored hash can be re-derived. Users upgrade one at a time as they sign in,
    without ever being asked to change anything.
    """
    user = await make_user(db_session)
    user.password_hash = _weak_hasher.hash(DEFAULT_PASSWORD)
    await db_session.flush()
    assert needs_rehash(user.password_hash), "the fixture is not actually stale"

    await _auth_service(db_session).login(LoginRequest(email=user.email, password=DEFAULT_PASSWORD))

    assert not needs_rehash(user.password_hash), "the hash was not upgraded"
    assert verify_password(DEFAULT_PASSWORD, user.password_hash), "the password still works"


# --------------------------------------------------------------------------
# Auth dependencies
# --------------------------------------------------------------------------


async def test_a_token_for_a_deleted_user_is_rejected(db_session: AsyncSession) -> None:
    """The signature is valid; the subject is not.

    This is why the dependency reads the database on every request rather than
    trusting the token's claims.
    """
    token = create_token(str(uuid.uuid4()), token_type="access", expires_in=FIVE_MINUTES)

    with pytest.raises(AuthenticationError):
        await get_current_user(_Credentials(token), db_session)  # type: ignore[arg-type]


async def test_a_token_for_a_deactivated_user_is_rejected(db_session: AsyncSession) -> None:
    """Deactivation takes effect immediately, unlike logout — see ADR-0004."""
    user = await make_user(db_session, is_active=False)
    token = create_token(str(user.id), token_type="access", expires_in=FIVE_MINUTES)

    with pytest.raises(AuthenticationError):
        await get_current_user(_Credentials(token), db_session)  # type: ignore[arg-type]


async def test_a_token_whose_subject_is_not_a_uuid_is_rejected(db_session: AsyncSession) -> None:
    """Only reachable with a forged-but-correctly-signed token — which means
    only if the signing key has leaked. It still must not raise a 500."""
    token = create_token("definitely-not-a-uuid", token_type="access", expires_in=FIVE_MINUTES)

    with pytest.raises(AuthenticationError):
        await get_current_user(_Credentials(token), db_session)  # type: ignore[arg-type]


async def test_a_missing_authorization_header_is_rejected(db_session: AsyncSession) -> None:
    """HTTPBearer is configured with `auto_error=False` so this reaches us and
    produces the same error body as every other failure."""
    with pytest.raises(AuthenticationError):
        await get_current_user(None, db_session)
