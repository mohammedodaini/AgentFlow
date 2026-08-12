"""/organizations — org CRUD + member management (invite, change role, remove)."""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentMembership, CurrentUser
from app.core.exceptions import NotFoundError
from app.db.deps import get_db
from app.schemas.organization import (
    MemberInvite,
    MemberRead,
    MemberRoleUpdate,
    MembershipRead,
    OrganizationCreate,
    OrganizationRead,
)
from app.services.organization_service import OrganizationService

router = APIRouter(prefix="/organizations", tags=["organizations"])

SessionDep = Annotated[AsyncSession, Depends(get_db)]


def _require_same_organization(membership: CurrentMembership, organization_id: uuid.UUID) -> None:
    """Reject a path id that disagrees with the X-Organization-Id header.

    Two sources for one value is a bug waiting to happen: authorize against the
    header, act on the path, and a member of org A can operate on org B by
    sending a header for A and a path for B. Rather than silently pick a
    winner, refuse — and answer 404, matching what the caller would have got
    for an organization they are not in.
    """
    if membership.organization_id != organization_id:
        message = "Organization not found"
        raise NotFoundError(message)


@router.post("", status_code=HTTPStatus.CREATED, summary="Create an organization")
async def create_organization(
    request: OrganizationCreate, user: CurrentUser, session: SessionDep
) -> OrganizationRead:
    """Create an organization; the caller becomes its owner.

    Only `CurrentUser`, not `CurrentMembership` — you cannot be scoped to an
    organization that does not exist yet. This is one of exactly two
    authenticated endpoints without a tenancy scope; the other is `/users/me`.
    """
    organization, _ = await OrganizationService(session).create(
        name=request.name, slug=request.slug, owner=user
    )
    return OrganizationRead.model_validate(organization)


@router.get("", summary="Organizations the caller belongs to")
async def list_my_organizations(user: CurrentUser, session: SessionDep) -> list[MembershipRead]:
    """Every workspace this person can act in, and their role in each.

    Deliberately *not* org-scoped: this is the endpoint a client calls before
    it knows which `X-Organization-Id` to send. Requiring the header here would
    be a chicken-and-egg problem.
    """
    memberships = await OrganizationService(session).list_for_user(user.id)
    return [MembershipRead.model_validate(membership) for membership in memberships]


@router.get("/{organization_id}/members", summary="Members of an organization")
async def list_members(
    organization_id: uuid.UUID, membership: CurrentMembership, session: SessionDep
) -> list[MemberRead]:
    """The roster. Any member may read it; only managers may change it."""
    _require_same_organization(membership, organization_id)

    rows = await OrganizationService(session).list_members(organization_id)
    return [
        MemberRead(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=member.role,
            joined_at=member.created_at,
        )
        for member, user in rows
    ]


@router.post(
    "/{organization_id}/members",
    status_code=HTTPStatus.CREATED,
    summary="Add an existing user to an organization",
)
async def add_member(
    organization_id: uuid.UUID,
    request: MemberInvite,
    membership: CurrentMembership,
    session: SessionDep,
) -> MemberRead:
    """Add a member. Requires owner or admin — enforced in the service.

    The role check is deliberately not written here. `require_role(...)` could
    guard this route, but the rule would then apply only to HTTP callers, while
    workers and agent tools calling `OrganizationService` directly would bypass
    it entirely. One rule, one place, all three callers.
    """
    _require_same_organization(membership, organization_id)

    created, user = await OrganizationService(session).invite_member(
        organization_id=organization_id,
        email=request.email,
        role=request.role,
        actor=membership,
    )

    return MemberRead(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=created.role,
        joined_at=created.created_at,
    )


@router.patch("/{organization_id}/members/{user_id}", summary="Change a member's role")
async def change_member_role(
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    request: MemberRoleUpdate,
    membership: CurrentMembership,
    session: SessionDep,
) -> MembershipRead:
    """Promote or demote a member.

    The interesting rules — an admin cannot grant or revoke ownership, and the
    last owner cannot be demoted — live in the service.
    """
    _require_same_organization(membership, organization_id)

    updated = await OrganizationService(session).change_role(
        organization_id=organization_id,
        target_user_id=user_id,
        role=request.role,
        actor=membership,
    )
    return MembershipRead.model_validate(updated)


@router.delete(
    "/{organization_id}/members/{user_id}",
    status_code=HTTPStatus.NO_CONTENT,
    summary="Remove a member, or leave",
)
async def remove_member(
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    membership: CurrentMembership,
    session: SessionDep,
) -> Response:
    """Remove someone, or leave yourself.

    One endpoint for both, because they are the same operation with different
    permissions: anyone may remove themselves, only managers may remove others,
    and nobody may remove the last owner.
    """
    _require_same_organization(membership, organization_id)

    await OrganizationService(session).remove_member(
        organization_id=organization_id, target_user_id=user_id, actor=membership
    )
    return Response(status_code=HTTPStatus.NO_CONTENT)
