"""`memberships` — users ⟷ organizations many-to-many WITH a role payload.

A real table (not a bare join): roles, invites, and seat-billing live here
later. Unique on (user_id, organization_id).

Imported by: auth/dependencies (org scoping), services/organization_service.
"""

from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.user import User


class Role(enum.StrEnum):
    """What a member may do inside one organization.

    `StrEnum` so a member compares equal to its wire value: `role == "owner"`
    is True, JSON serialisation is free, and no `.value` litters the call sites.
    """

    OWNER = "owner"
    """Billing and deletion. Exactly one per organization, enforced in M3."""

    ADMIN = "admin"
    """Invites members and manages integrations. Cannot delete the org."""

    MEMBER = "member"
    """Uses the product. The default for anyone who accepts an invite."""


class Membership(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One person's seat in one organization.

    Why this is a table rather than a `users.organization_id` column: a column
    caps every person at one organization forever. Consultants, agencies, and
    anyone with a personal workspace plus an employer break that on day one —
    and retrofitting tenancy afterwards means rewriting every query in the
    application. A join table also gives the role somewhere to live, and later
    the invite state and seat billing.
    """

    __tablename__ = "memberships"
    __table_args__ = (
        # The application cannot enforce this. Two concurrent invite requests
        # both read "not a member yet" and both insert; only the database sees
        # them at the same instant.
        UniqueConstraint("user_id", "organization_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    """Deliberately *not* `index=True`.

    The unique constraint below already creates an index on
    `(user_id, organization_id)`, and Postgres can use a composite index for
    lookups on its leading column. A second index on `user_id` alone would be
    dead weight: more storage, and a write cost paid on every insert forever.
    """

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    """This one *does* need its own index — it is the trailing column of the
    composite, which the composite index cannot serve. "Who is in this org?"
    runs on nearly every request once org scoping lands in M3."""

    role: Mapped[Role] = mapped_column(
        Enum(
            Role,
            name="membership_role",
            # Store "owner", not "OWNER". Without values_callable SQLAlchemy
            # persists the *member name*, so the database and the JSON API
            # would disagree about what a role is called.
            values_callable=lambda enum_class: [member.value for member in enum_class],
        ),
        nullable=False,
        default=Role.MEMBER,
    )
    """A native Postgres enum, which buys database-level validation.

    The cost, honestly: adding a value later needs `ALTER TYPE ... ADD VALUE`
    in its own migration step, and removing one needs a full type rebuild. The
    alternative — VARCHAR plus a CHECK constraint — is easier to change and
    weaker to read. Roles change roughly never, so validation wins here; the
    same reasoning is why `Organization.plan` is a plain string.
    """

    user: Mapped[User] = relationship(back_populates="memberships")
    organization: Mapped[Organization] = relationship(back_populates="memberships")

    def __repr__(self) -> str:
        return f"<Membership user={self.user_id} org={self.organization_id} role={self.role}>"
