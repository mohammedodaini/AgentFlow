"""/memories — what the agent has learned, and nothing that writes it (M10).

Layer: api.

Read-only, and that is the whole design of this module. Memories are written by
one path — extraction, in a worker, from a real conversation — and giving the API
a `POST` would create a second: a way to inject an assertion the agent then
treats as something it learned, with no conversation behind it and nothing to
audit. The provenance column (`source_run_id`) would be null and honest, and
nobody would look.

It exists because long-term memory is the one feature here that changes answers
without being asked to. A retrieved document is cited and checkable; a memory is
not. A store nobody can inspect is an unreviewable input, so the inspection
surface ships in the same milestone as the first writer rather than after
somebody needs it in anger.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentMembership
from app.db.deps import get_db
from app.repositories.memory_repository import MemoryRepository
from app.schemas.common import Page
from app.schemas.memory import MemoryRead

router = APIRouter(prefix="/memories", tags=["memory"])

SessionDep = Annotated[AsyncSession, Depends(get_db)]


@router.get("", summary="List what the agent remembers")
async def list_memories(
    membership: CurrentMembership,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[MemoryRead]:
    """This organization's shared memories, plus your own — never anyone else's.

    The same visibility predicate recall uses, which is the point: an inspection
    view showing *more* than the agent can act on would be a privacy hole dressed
    as a debugging tool, and one showing less would hide exactly the memory
    somebody is trying to explain.

    A repository is used directly rather than through a service, and this is the
    one endpoint in the codebase where that is right: there is no business logic
    to own. Wrapping a paged read in a service class would add a layer whose only
    method forwards its arguments unchanged.
    """
    memories = MemoryRepository(session)

    return Page[MemoryRead](
        items=[
            MemoryRead.model_validate(memory)
            for memory in await memories.list_for_organization(
                membership.organization_id,
                user_id=membership.user_id,
                limit=limit,
                offset=offset,
            )
        ],
        total=await memories.count_for_organization(
            membership.organization_id, user_id=membership.user_id
        ),
        limit=limit,
        offset=offset,
    )
