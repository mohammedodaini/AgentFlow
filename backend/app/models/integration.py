"""`integrations` — one row per connected external product per organization.

Layer: models. The metadata half of a connection; `oauth_tokens` holds the
secrets, and the split is the point (see that module).

Status is a lifecycle, not a boolean
------------------------------------
The tempting design is `is_connected`. It cannot express the state that actually
matters in production: **connected, but no longer working.** People revoke access
from their Google account page, change password policies, or leave the company,
and none of that reaches us as an event — we discover it the next time a refresh
fails.

A boolean forces that discovery into one of two lies. Flip it to false and the
integration looks like it was never set up, so nobody knows what broke or that it
used to work. Leave it true and the agent keeps trying a credential that will
never succeed again, failing every task that depends on it with an error the user
cannot act on.

`REVOKED` is neither. It says: this was real, it is broken, and reconnecting is
the fix — which is the only thing a user can usefully be told.
"""

from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ARRAY, Enum, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.oauth_token import OAuthToken


class Provider(enum.StrEnum):
    """Which external product a row connects.

    The full set from `docs/database.md` is declared now even though M11
    implements one, for the reason `RunStatus.PAUSED_FOR_APPROVAL` was (M9):
    adding a value to a native Postgres enum later is an `ALTER TYPE`, and a
    provider the application can write but the database rejects is a failure that
    only appears when somebody connects the second integration.

    `google_calendar` rather than `google`, because scopes and refresh behaviour
    differ per Google product — and one row per product is what lets a user
    connect Calendar without also handing over their Drive.
    """

    GMAIL = "gmail"
    GOOGLE_CALENDAR = "google_calendar"
    GOOGLE_DRIVE = "google_drive"
    SLACK = "slack"
    NOTION = "notion"
    GITHUB = "github"
    STRIPE = "stripe"


class IntegrationStatus(enum.StrEnum):
    """Whether this connection can currently be used."""

    ACTIVE = "active"

    REVOKED = "revoked"
    """The credential no longer works and no retry will fix it.

    Set when a refresh returns `invalid_grant` — Google's answer for a refresh
    token that was revoked, expired through disuse, or invalidated by a password
    change. It is a *normal* state, reached without anything going wrong on our
    side, and the only honest response is to ask the user to reconnect.
    """

    DISCONNECTED = "disconnected"
    """The user turned it off here. Kept rather than deleted so the audit trail
    survives: "who connected our Google Calendar, and when was it removed?" is a
    question a deleted row cannot answer."""


class Integration(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One connected product, for one organization."""

    __tablename__ = "integrations"
    __table_args__ = (
        # One live connection per product per organization. Two would mean two
        # sets of tokens and no rule for which a tool should use — so the first
        # ambiguous refresh silently picks one and the other rots.
        #
        # Scoped to ACTIVE only, via a partial index, because disconnected and
        # revoked rows are history and there can be many: a user who connects,
        # disconnects and reconnects must not be blocked by their own audit
        # trail.
        Index(
            "uq_integrations_organization_id_provider_active",
            "organization_id",
            "provider",
            unique=True,
            postgresql_where="status = 'active'",
        ),
        Index("ix_integrations_organization_id_status", "organization_id", "status"),
        UniqueConstraint("id", "organization_id", name="uq_integrations_id_organization_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    """The tenant. An integration belongs to the *company*, not to the person who
    clicked connect — which is why `connected_by` is separate and nullable."""

    provider: Mapped[Provider] = mapped_column(
        Enum(
            Provider,
            name="integration_provider",
            native_enum=True,
            values_callable=lambda enum_class: [member.value for member in enum_class],
        ),
        nullable=False,
    )

    status: Mapped[IntegrationStatus] = mapped_column(
        Enum(
            IntegrationStatus,
            name="integration_status",
            native_enum=True,
            values_callable=lambda enum_class: [member.value for member in enum_class],
        ),
        nullable=False,
        default=IntegrationStatus.ACTIVE,
    )

    connected_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    """Who authorised it. `SET NULL` rather than `CASCADE`, the same rule as
    `agent_runs.triggered_by`: when someone leaves, the record that the company's
    calendar was connected — and by whom — must outlive their account.

    Worth being clear-eyed about what this does *not* mean. The credential was
    granted from that person's Google account, so their departure is usually
    exactly when it stops working. This column is the audit trail; the `REVOKED`
    status is what the product does about it.
    """

    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(200)), nullable=False, default=list)
    """What the user actually granted, as returned by the provider.

    Stored from the *token response*, never from what we asked for. A user can
    untick a permission on the consent screen and Google returns the reduced set
    without complaint — so a system that recorded its own request would believe it
    had access it does not have, and would fail at the first call rather than at
    the point where a decision could be made about it.
    """

    external_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """Which account on the provider's side this is — an email address for Google.

    Shown to the user, because "your Google Calendar is connected" is unhelpful to
    anyone with two Google accounts, and it is how somebody notices they connected
    the wrong one.
    """

    tokens: Mapped[OAuthToken | None] = relationship(
        back_populates="integration",
        cascade="all, delete-orphan",
        uselist=False,
    )
    """One row, not many. Refreshing replaces the credential in place rather than
    appending — keeping superseded access tokens would mean storing expired
    secrets whose only remaining use is being leaked."""
