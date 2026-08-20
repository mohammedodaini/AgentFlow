"""`events` — the append-only audit log.

Layer: models. Every security-relevant thing that happened, and who did it.

Why this is separate from the structured logs
----------------------------------------------
`structlog` already records requests, and it is not an audit log. Logs are
operational: they rotate, they are sampled under load, they are shipped to a
third party, and nobody promises they are complete. An audit trail has to answer
"who connected our Stripe account, and when?" a year later, after the log
retention window closed and the person left.

So this is a table, in the same database, inside the same transaction as the
thing it records. An event and the change it describes commit together or not at
all — which is the one property a log file can never have.

Append-only, and the model says so
-----------------------------------
There is no `updated_at` here, and its absence is deliberate rather than an
oversight. Every other table in this schema carries `TimestampMixin`; a column
recording when a row *changed* would imply these rows change, and an audit entry
that can be edited is not evidence. Nothing in the application updates or deletes
one, and `EventRepository` exposes no method that could.

The actor is a person **or** an agent
--------------------------------------
Two nullable foreign keys rather than one polymorphic column, because agents act
in this system and "the calendar agent created an event" is exactly the kind of
thing an audit trail exists to record. Both nullable, and both `SET NULL`: when
somebody leaves, the record of what they did must outlive their account, which is
the difference between an audit trail and a convenience.

`organization_id` is nullable too, for a narrower reason. A failed sign-in has no
organization — the whole point is that the credential did not resolve to one — and
inventing an organization for it would put a security event in a tenant's history
on the strength of an attacker's guess at an email address.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class EventType(enum.StrEnum):
    """What happened.

    **A Python enum, but stored as a plain `String` column** — the opposite of
    every other enum in this schema, and the reason is that this one is a *growing
    vocabulary* rather than a closed domain. `membership_role` has three values
    because there are three roles; audit event types grow with every feature, and
    a native Postgres enum would make "record one more kind of event" an
    `ALTER TYPE` in a migration.

    That trade is the mirror of `agent_runs.agent_name` (M9): a typo becomes an
    event nobody can filter for, which is what this enum exists to prevent on the
    write side while leaving the column permissive.

    **Only values something actually emits appear here.** A draft of this enum
    also declared `document.deleted`, which nothing writes — the audit equivalent
    of a dashboard panel that never fills, where a reader cannot tell "this never
    happened" from "this is not recorded", and the second reading is the dangerous
    one. Adding a value is a deploy, so there is no cost to waiting until
    something emits it.

    The membership values are here because M3 left five `TODO(M16)` markers in
    `auth/service.py` and `services/organization_service.py` saying exactly where
    an audit row belonged. Those are the rows.
    """

    USER_REGISTERED = "user.registered"
    USER_SIGNED_IN = "user.signed_in"
    USER_SIGN_IN_FAILED = "user.sign_in_failed"
    USER_SIGNED_OUT = "user.signed_out"

    INTEGRATION_CONNECTED = "integration.connected"
    INTEGRATION_DISCONNECTED = "integration.disconnected"
    INTEGRATION_REVOKED = "integration.revoked"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_APPROVED = "approval.approved"
    APPROVAL_REJECTED = "approval.rejected"

    DOCUMENT_UPLOADED = "document.uploaded"

    ORGANIZATION_CREATED = "organization.created"
    MEMBER_INVITED = "member.invited"
    MEMBER_ROLE_CHANGED = "member.role_changed"
    MEMBER_REMOVED = "member.removed"


class Event(UUIDPrimaryKeyMixin, Base):
    """One thing that happened, recorded permanently."""

    __tablename__ = "events"
    __table_args__ = (
        # Every read of this table is "what happened in this organization,
        # newest first" — for a compliance export or an incident. Without the
        # composite index the planner filters by tenant and sorts the whole
        # result, which is fine at a thousand rows and not at ten million.
        Index("ix_events_organization_id_created_at", "organization_id", "created_at"),
        # The second question anyone asks: "what did this person do?" Almost
        # always scoped to a tenant as well, so the actor leads and the tenant
        # follows.
        Index("ix_events_actor_user_id_created_at", "actor_user_id", "created_at"),
        # And the third: "show me every failed sign-in." A type-led index,
        # because that query cannot use either of the two above.
        Index("ix_events_event_type_created_at", "event_type", "created_at"),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True
    )
    """The tenant, or NULL for an event that belongs to no organization.

    `CASCADE` here and nowhere else in this table: deleting an organization is
    the one operation that should take its audit trail, because the trail is
    *their* data and retaining it after they leave is a retention problem rather
    than a compliance benefit.
    """

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    """Who did it, if a person did. `SET NULL` so the record outlives them."""

    actor_agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    """Which run did it, if an agent did.

    Both actor columns can be set at once, and that is the *interesting* case
    rather than a contradiction: an approved calendar write was performed by a run
    that a person authorised, and an audit trail that could only name one of them
    would lose whichever half the reader needed.
    """

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    """An `EventType` value. See that enum for why the column is a string."""

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    """What is worth knowing about this event, and nothing more.

    **No secrets, no message bodies, no document contents.** An audit trail is
    read by people who were never authorised to read the *data* — auditors,
    support, whoever is handling an incident — and its value comes from being
    widely readable. A payload carrying an email body turns the one table
    everybody can see into the one place everything leaks.

    So: ids, names, statuses, counts. `EventService` is where that rule is
    applied, because a rule enforced at the call site is a rule somebody skips.
    """

    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    """Where the request came from, for sign-in events.

    45 characters: the longest possible IPv6 text form is an IPv4-mapped address
    like `0000:...:255.255.255.255`, which is 45. A `String(39)` sized for plain
    IPv6 silently truncates those into a *different, valid-looking* address, which
    is worse than storing nothing.

    Not `INET`, despite Postgres having it, because this column is never compared
    or subnet-matched — it is displayed. A type whose only benefit is operators
    nobody uses costs a driver conversion on every read.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    """When, from the *database's* clock rather than the application's.

    `server_default=func.now()` and no Python default, deliberately. Every other
    table here takes its timestamp from the process, which is fine for data the
    process owns. An audit trail is evidence, and evidence should not be
    timestamped by the thing being audited: an application server with a skewed
    clock — or a compromised one — would otherwise write history in whatever order
    it liked.

    There is also no `updated_at`. See the module docstring: a column recording
    when a row changed would imply these rows change.
    """
