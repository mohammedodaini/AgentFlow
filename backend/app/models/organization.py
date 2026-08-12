"""`organizations` table — THE tenant. Almost every other table FKs here.

Multi-tenancy rule: queries are scoped by organization_id, never by user_id.

Imported by: app/services/organization_service.py, app/auth/service.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    # Type-checking only: models/membership.py imports this module back, and a
    # runtime import would be a cycle. SQLAlchemy resolves the string
    # annotation lazily, so it never needs the real class at import time.
    from app.models.membership import Membership


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A customer account — the unit of tenancy, billing and data isolation.

    Everything the product stores (documents, conversations, agent runs) hangs
    off an organization rather than a user, because a company's data has to
    outlive any individual employee's account.
    """

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    """URL-safe identifier, e.g. `acme-corp`.

    Unique because it appears in URLs and invite links. Kept separate from
    `name` so a company can rename itself without breaking every existing link.
    """

    plan: Mapped[str] = mapped_column(String(50), nullable=False, default="free")
    """Billing tier. A plain string, not an enum — pricing tiers change far more
    often than roles do, and every change to a Postgres enum is a migration."""

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    """Staff of this organization.

    `passive_deletes=True` lets the database's ON DELETE CASCADE do the work
    instead of SQLAlchemy loading every child row into memory to delete it one
    at a time. On a large org that is one statement versus thousands.
    """

    def __repr__(self) -> str:
        return f"<Organization {self.slug}>"
