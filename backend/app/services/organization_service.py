"""Org + membership business logic: create org, invite, change role, remove.

Enforces role rules (only owner|admin manage members; last owner cannot leave)
and writes `events` audit rows for every mutation.

Note on that last clause: the `events` table does not exist until M16, so the
audit rows are TODOs below rather than a silent omission. Every mutating method
here is a candidate.

Where the rules live
--------------------
Role checks are enforced *here*, not only in the route dependencies. Routes are
one of three callers — background workers and agent tools are the others, and
neither passes through a FastAPI dependency. A rule that exists only in the
transport layer is a rule that does not exist for two thirds of the callers.
"""

from __future__ import annotations

import re
import uuid

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError
from app.models.membership import Membership, Role
from app.models.organization import Organization
from app.models.user import User

logger = structlog.get_logger(__name__)

MANAGER_ROLES = frozenset({Role.OWNER, Role.ADMIN})
"""Who may manage members at all. Ordinary members can read the roster and
nothing else — seeing colleagues is not the same as being able to remove one."""

MAX_SLUG_ATTEMPTS = 100
"""Bound on the dedupe loop. An unbounded `while True` around a database query
is how a service hangs forever instead of failing."""


def slugify(value: str) -> str:
    """Turn a display name into a URL-safe slug.

    `"Acme Corp. (EU)"` -> `"acme-corp-eu"`. Empty input, or input with no
    usable characters at all, yields `"org"` — a slug is required, and failing
    a registration because someone named their company `"???"` would be absurd.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:90] or "org"


class OrganizationService:
    """Organizations and who belongs to them.

    Takes a session rather than creating one: the caller owns the transaction
    (see `app/db/deps.py`), which is what lets `AuthService.register` create a
    user, an organization and a membership as one atomic unit.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- reads ------------------------------------------------------------

    async def get_membership(self, user_id: uuid.UUID, organization_id: uuid.UUID) -> Membership:
        """The caller's seat in one organization — the tenancy check itself.

        Raises `NotFoundError`, not `AuthorizationError`, when the caller is not
        a member. "You are not in this organization" and "no such organization"
        must be indistinguishable, or the endpoint becomes an oracle for
        enumerating which organization ids exist.
        """
        membership = await self._session.scalar(
            select(Membership)
            .where(Membership.user_id == user_id)
            .where(Membership.organization_id == organization_id)
            .options(selectinload(Membership.organization))
        )

        if membership is None:
            message = "Organization not found"
            raise NotFoundError(message)

        return membership

    async def list_for_user(self, user_id: uuid.UUID) -> list[Membership]:
        """Every organization this person belongs to, with their role in each.

        `selectinload` because the caller serialises `membership.organization`.
        Without it each row triggers its own lazy load — the N+1 problem — and
        under asyncio a lazy load outside the session raises `MissingGreenlet`
        rather than merely being slow.
        """
        result = await self._session.scalars(
            select(Membership)
            .where(Membership.user_id == user_id)
            .options(selectinload(Membership.organization))
            .order_by(Membership.created_at)
        )
        return list(result)

    async def list_members(self, organization_id: uuid.UUID) -> list[tuple[Membership, User]]:
        """The roster. One join, not one query per member."""
        result = await self._session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.organization_id == organization_id)
            .order_by(Membership.created_at)
        )
        return [(membership, user) for membership, user in result.all()]

    # -- writes -----------------------------------------------------------

    async def create(
        self, *, name: str, owner: User, slug: str | None = None
    ) -> tuple[Organization, Membership]:
        """Create an organization and make `owner` its owner.

        Returns both, because the caller almost always needs the membership
        too — an organization with no members is not a state this application
        should be able to produce, even for a moment.

        Flushes rather than commits: the transaction belongs to the request.
        """
        organization = Organization(name=name, slug=await self._unique_slug(slug or slugify(name)))
        self._session.add(organization)
        await self._session.flush()

        membership = Membership(user_id=owner.id, organization_id=organization.id, role=Role.OWNER)
        self._session.add(membership)
        await self._session.flush()

        logger.info(
            "organization.created",
            organization_id=str(organization.id),
            slug=organization.slug,
            owner_id=str(owner.id),
        )
        # TODO(M16): write an `events` audit row here.
        return organization, membership

    async def invite_member(
        self, *, organization_id: uuid.UUID, email: str, role: Role, actor: Membership
    ) -> tuple[Membership, User]:
        """Add an existing user to an organization.

        Raises `NotFoundError` if no account has that email — M3 does not send
        invitations to strangers (see `MemberInvite`).

        Returns the user alongside the membership because the caller needs both
        to render a roster row. Handing back only the membership would force the
        route to reach for `membership.user`, and that relationship is not
        loaded — under asyncio a lazy load outside the session raises
        `MissingGreenlet` rather than quietly issuing a second query.
        """
        self._require_manager(actor)
        self._require_not_escalating(actor, target_role=role)

        user = await self._session.scalar(select(User).where(User.email == email))
        if user is None:
            message = "No user with that email"
            raise NotFoundError(message)

        existing = await self._session.scalar(
            select(Membership)
            .where(Membership.user_id == user.id)
            .where(Membership.organization_id == organization_id)
        )
        if existing is not None:
            message = "That user is already a member"
            raise ConflictError(message)

        membership = Membership(user_id=user.id, organization_id=organization_id, role=role)
        self._session.add(membership)
        await self._session.flush()

        logger.info(
            "organization.member_added",
            organization_id=str(organization_id),
            user_id=str(user.id),
            role=role.value,
            actor_id=str(actor.user_id),
        )
        # TODO(M16): write an `events` audit row here.
        return membership, user

    async def change_role(
        self,
        *,
        organization_id: uuid.UUID,
        target_user_id: uuid.UUID,
        role: Role,
        actor: Membership,
    ) -> Membership:
        """Change what an existing member may do."""
        self._require_manager(actor)
        self._require_not_escalating(actor, target_role=role)

        target = await self.get_membership(target_user_id, organization_id)
        self._require_can_act_on(actor, target)

        if target.role is Role.OWNER and role is not Role.OWNER:
            await self._require_not_last_owner(organization_id)

        target.role = role
        await self._session.flush()

        logger.info(
            "organization.role_changed",
            organization_id=str(organization_id),
            user_id=str(target_user_id),
            role=role.value,
            actor_id=str(actor.user_id),
        )
        # TODO(M16): write an `events` audit row here.
        return target

    async def remove_member(
        self, *, organization_id: uuid.UUID, target_user_id: uuid.UUID, actor: Membership
    ) -> None:
        """Remove someone from an organization.

        Leaving voluntarily is allowed for anyone; removing *somebody else*
        needs a manager role. That asymmetry is why the manager check is
        skipped when the actor is the target.
        """
        target = await self.get_membership(target_user_id, organization_id)
        removing_self = actor.user_id == target_user_id

        if not removing_self:
            self._require_manager(actor)
            self._require_can_act_on(actor, target)

        if target.role is Role.OWNER:
            await self._require_not_last_owner(organization_id)

        await self._session.delete(target)
        await self._session.flush()

        logger.info(
            "organization.member_removed",
            organization_id=str(organization_id),
            user_id=str(target_user_id),
            actor_id=str(actor.user_id),
            self_removal=removing_self,
        )
        # TODO(M16): write an `events` audit row here.

    # -- rules ------------------------------------------------------------

    @staticmethod
    def _require_manager(actor: Membership) -> None:
        if actor.role not in MANAGER_ROLES:
            message = "Only owners and admins can manage members"
            raise AuthorizationError(message)

    @staticmethod
    def _require_not_escalating(actor: Membership, *, target_role: Role) -> None:
        """An admin may not hand out ownership.

        Without this, "admin" and "owner" are the same role with different
        names: any admin could promote themselves — or an accomplice — to owner
        and then remove the real owner.
        """
        if target_role is Role.OWNER and actor.role is not Role.OWNER:
            message = "Only an owner can grant ownership"
            raise AuthorizationError(message)

    @staticmethod
    def _require_can_act_on(actor: Membership, target: Membership) -> None:
        """An admin may not modify or remove an owner.

        The mirror image of the rule above, closing the other direction of the
        same attack: demote-then-promote is escalation in two steps.
        """
        if target.role is Role.OWNER and actor.role is not Role.OWNER:
            message = "Only an owner can modify another owner"
            raise AuthorizationError(message)

    async def _require_not_last_owner(self, organization_id: uuid.UUID) -> None:
        """Refuse to leave an organization ownerless.

        An org with no owner cannot be billed, renamed or deleted, and nobody
        can appoint a new owner — a support ticket only a database console can
        resolve. Cheaper to forbid.
        """
        owner_count = await self._session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.organization_id == organization_id)
            .where(Membership.role == Role.OWNER)
        )

        if (owner_count or 0) <= 1:
            message = "An organization must keep at least one owner"
            raise ConflictError(message)

    async def _unique_slug(self, base: str) -> str:
        """Append `-2`, `-3`, ... until the slug is free.

        Note the honest limitation: this is check-then-insert, so two
        simultaneous requests can both see `acme` as free. The database's UNIQUE
        constraint is what actually guarantees correctness — this loop only
        makes the common case produce a pleasant slug instead of a 409.
        """
        for suffix in range(1, MAX_SLUG_ATTEMPTS):
            candidate = base if suffix == 1 else f"{base}-{suffix}"
            taken = await self._session.scalar(
                select(Organization.id).where(Organization.slug == candidate)
            )
            if taken is None:
                return candidate

        message = "Could not allocate a unique slug"
        raise ConflictError(message)
