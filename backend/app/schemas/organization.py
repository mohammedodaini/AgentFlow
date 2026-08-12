"""Organization + membership API shapes."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.membership import Role
from app.schemas.common import APIModel, NormalizedEmail

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
"""Lowercase words joined by single hyphens: `acme`, `acme-corp`.

Enforced with a pattern rather than "whatever the user typed, lowercased",
because a slug ends up in URLs and invite links. Rejecting `../admin` and
`acme%20corp` at the boundary is cheaper than discovering what they do to a
route matcher.
"""


class OrganizationCreate(BaseModel):
    """Create an organization. The caller becomes its owner."""

    name: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=100, pattern=SLUG_PATTERN)
    """Optional: the service derives one from `name` when it is omitted, and
    appends a suffix if that slug is taken. Clients should not have to guess
    what is free."""


class OrganizationRead(APIModel):
    """An organization as the API presents it."""

    id: uuid.UUID
    name: str
    slug: str
    plan: str
    created_at: datetime


class MembershipRead(APIModel):
    """An organization *plus* what the caller may do in it.

    Returned by `GET /organizations`, which answers "which workspaces do I
    belong to, and as what?" — two questions the UI needs answered together,
    because the answer decides which buttons it renders.
    """

    organization: OrganizationRead
    role: Role


class MemberRead(APIModel):
    """One person's seat, flattened for the members list.

    Flattened deliberately: a members table shows a name, an email and a role,
    so nesting a whole `UserRead` inside a whole `MembershipRead` would make
    every client dig through two levels to render one row.
    """

    user_id: uuid.UUID
    email: NormalizedEmail
    full_name: str | None
    role: Role
    joined_at: datetime


class MemberInvite(BaseModel):
    """Add an existing user to an organization.

    M3 scope: the invitee must already have an account. Inviting a stranger
    means emailing a signed, expiring invitation and holding pending state —
    a flow, a token type and a table of its own, and none of that belongs in
    the milestone that has only just learned how to log someone in.
    """

    email: NormalizedEmail
    role: Role = Role.MEMBER
    """Defaults to the least privilege. An invite that forgets to name a role
    must never grant more than the minimum."""


class MemberRoleUpdate(BaseModel):
    """Change an existing member's role."""

    role: Role
