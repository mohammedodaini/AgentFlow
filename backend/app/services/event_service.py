"""Writing the audit trail, and deciding what is safe to keep in it.

Layer: services. The only writer of `events`, and the only place that knows what
belongs in a payload.

**One writer, because the rule lives here.** An audit payload must never carry a
secret, a message body or a document's contents — see `Event.payload` — and a rule
applied at each call site is a rule somebody skips on the Friday they add a
feature. Every caller hands this service an already-narrow set of facts, and
`_safe` strips anything that looks like a credential before it reaches SQL.

**The event commits with the thing it records.** `record()` adds a row to the
caller's session and does not commit. That is the entire reason this is a table
rather than a log line: an integration that connected and an event saying so
either both happen or neither does. A logger cannot promise that, and an audit
trail with gaps is worse than none — it invites someone to conclude that an
absent entry means an absent action.

**Recording never breaks the operation being recorded.** `record` is called on
paths that matter — sign-in, connecting an account, approving a side effect — and
an audit write that raised would turn a working feature into an outage. So the
one thing this service refuses to do is fail loudly. It logs and moves on, and the
missing row is a bug to fix rather than a request the user could not complete.

That is a genuine trade and it is the *opposite* of what a compliance-first system
would choose, where an unauditable action must not proceed. It is written here so
that the day this project needs the other behaviour, the change is one function
and the reasoning is already on the page.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event, EventType

logger = structlog.get_logger(__name__)

REDACTED = "[redacted]"

SENSITIVE_KEYS = frozenset(
    {
        "password",
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "client_secret",
        "api_key",
        "authorization",
        "body",
        "content",
    }
)
"""Keys whose values never reach the audit table.

A denylist, which is the weaker of the two designs and the right one here.
An allowlist would need a schema per event type — twenty of them, growing — and
the failure mode of a *missing* allowlist entry is an event with no useful detail,
which nobody notices. The failure mode here is louder: a new sensitive key that is
not on this list, which a reviewer can see in the diff that adds it.

`body` and `content` are on the list for M14's sake specifically: an email body
belongs in `approvals.requested_action`, which is tenant-scoped, and not in the
one table support staff and auditors read.
"""

MAX_VALUE_LENGTH = 500
"""Payload values are facts, not documents. A 40-page extract in an audit row is
somebody having passed the wrong variable."""


class EventService:
    """Appends to the audit trail. Cannot update or delete."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        event_type: EventType,
        *,
        organization_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        actor_agent_run_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        **payload: Any,
    ) -> None:
        """Add one event to the caller's transaction.

        Flushed, never committed: the caller owns the transaction boundary
        (`get_db`), and an audit row that committed itself would survive a request
        that was later rolled back — recording something that did not happen,
        which is a worse failure than recording nothing.
        """
        try:
            self._session.add(
                Event(
                    organization_id=organization_id,
                    actor_user_id=actor_user_id,
                    actor_agent_run_id=actor_agent_run_id,
                    event_type=event_type.value,
                    payload=_safe(payload),
                    ip_address=ip_address,
                )
            )
            await self._session.flush()
        except Exception:
            # See the module docstring. An audit write must not break the thing it
            # audits, and this is the one place in the codebase where swallowing an
            # exception is the considered choice rather than the lazy one.
            logger.exception("audit.record_failed", event_type=event_type.value)

    async def record_now(
        self,
        event_type: EventType,
        *,
        organization_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        **payload: Any,
    ) -> None:
        """Record an event on a path that is **about to raise**, and commit it.

        `record()` flushes and lets the caller's transaction decide, which is
        right for an event describing a change that succeeded. It is exactly
        wrong for a failure: `get_db` rolls the session back on any exception, so
        an event written just before `raise AuthenticationError` is discarded by
        the very failure it documents.

        That would silently lose the most valuable entry in the whole table.
        `user.sign_in_failed` is what makes credential stuffing visible — one row
        per guess, from one address, in one minute — and a trail holding every
        successful sign-in and no failed ones is worse than useless, because it
        looks complete.

        This is M14's `_mark_revoked` bug and M12's approval-decision bug for the
        third time, and the rule they share is now written down once: **a fact
        learned about the outside world must survive the failure it records.**

        Safe to commit here because these paths have nothing else pending —
        a rejected sign-in changed nothing that a commit could prematurely
        publish.
        """
        await self.record(
            event_type,
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            ip_address=ip_address,
            **payload,
        )

        try:
            await self._session.commit()
        except Exception:
            logger.exception("audit.commit_failed", event_type=event_type.value)

    async def list_for_organization(
        self, organization_id: uuid.UUID, *, limit: int = 50, event_type: EventType | None = None
    ) -> list[Event]:
        """The trail for one tenant, newest first.

        Tenant-scoped in SQL rather than filtered in Python, like every other read
        in this codebase — an audit log that could be read across tenants would be
        the most valuable single endpoint in the system to an attacker.
        """
        statement = (
            select(Event)
            .where(Event.organization_id == organization_id)
            .order_by(Event.created_at.desc())
            .limit(limit)
        )

        if event_type is not None:
            statement = statement.where(Event.event_type == event_type.value)

        return list((await self._session.scalars(statement)).all())


def _safe(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip anything that must not be kept, and bound what is left.

    Recursive, because a nested dict is exactly how a credential arrives without
    anybody meaning it to — `{"grant": {"access_token": "..."}}` passes a
    top-level check and fails the purpose.
    """
    clean: dict[str, Any] = {}

    for key, value in payload.items():
        if key.lower() in SENSITIVE_KEYS:
            clean[key] = REDACTED
            continue

        if isinstance(value, dict):
            clean[key] = _safe(value)
            continue

        if isinstance(value, str) and len(value) > MAX_VALUE_LENGTH:
            clean[key] = value[:MAX_VALUE_LENGTH] + "…"
            continue

        if isinstance(value, uuid.UUID):
            # JSONB cannot hold a UUID object, and `json.dumps` raises on one —
            # which would take the whole request down through a path that exists
            # to be harmless.
            clean[key] = str(value)
            continue

        clean[key] = value

    return clean
