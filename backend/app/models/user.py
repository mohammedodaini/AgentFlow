"""`users` table — auth identity ONLY; business data hangs off organizations.

Imported by: auth/, services/user_service, models/membership (relationship).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.membership import Membership


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A person who can log in.

    Deliberately thin. The temptation is to hang preferences, documents and
    settings here; resist it. A user is an *identity*, and identities move
    between organizations. Anything a company would consider its own data
    belongs on Organization.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    """320 = RFC 5321's maximum: 64-char local part + `@` + 255-char domain.

    Indexed because every login and every token validation looks a user up by
    email. Stored lowercased by the service layer (M3) — Postgres comparison is
    case-sensitive, so `Bob@x.com` and `bob@x.com` would otherwise be two
    accounts that both satisfy the unique constraint.
    """

    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    """Argon2id digest, produced in M3. Never a plaintext password, ever.

    Named `password_hash` rather than `password` on purpose: the name is the
    last line of defence against someone assigning to it directly.
    """

    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """Nullable — collected at signup when offered, never required to log in."""

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    """Soft disable. Deleting a user would orphan their audit trail;
    deactivating keeps the history intact while blocking every login."""

    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """Email ownership confirmed. Gates invites and outbound email later."""

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    """Organizations this person belongs to — a consultant may have several."""

    def __repr__(self) -> str:
        return f"<User {self.email}>"
