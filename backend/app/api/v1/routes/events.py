"""`/events` — reading the audit trail.

Layer: api.

**Owners and admins only.** The trail records who signed in, from where, who
connected which account and who authorised which side effect. That is exactly the
material an attacker with a foothold wants: it maps the organization, names the
people who matter, and shows which addresses are normal for them. A member has no
reason to read it, and `require_role` is where that is enforced.

There is no write endpoint, and there never should be. The only writer is
`EventService`, called from the services that perform the actions — an API that
let a client post an audit entry would let a client post a *false* one, which
turns evidence into testimony.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentMembership, require_role
from app.db.deps import get_db
from app.models.event import EventType
from app.models.membership import Role
from app.schemas.event import EventRead
from app.services.event_service import EventService

router = APIRouter(prefix="/events", tags=["audit"])

SessionDep = Annotated[AsyncSession, Depends(get_db)]


@router.get(
    "",
    summary="Read the audit trail",
    dependencies=[Depends(require_role(Role.OWNER, Role.ADMIN))],
)
async def list_events(
    membership: CurrentMembership,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    event_type: EventType | None = None,
) -> list[EventRead]:
    """What happened in this organization, newest first.

    Tenant-scoped in SQL, like every read in this codebase. An audit log readable
    across tenants would be the single most valuable endpoint in the system to an
    attacker — it would name every customer, their staff, and their habits.

    `event_type` is typed as the enum rather than a string, so an unknown value is
    a 422 naming the valid ones instead of an empty list. An empty list is the
    worst possible answer to a typo'd filter: it reads as "nothing happened".
    """
    events = await EventService(session).list_for_organization(
        membership.organization_id, limit=limit, event_type=event_type
    )
    return [EventRead.model_validate(event) for event in events]
