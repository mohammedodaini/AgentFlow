# ruff: noqa: F401  — remove once this module is implemented (M3)
"""Org + membership business logic: create org, invite, change role, remove.

Enforces role rules (only owner|admin manage members; last owner cannot leave)
and writes `events` audit rows for every mutation.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthorizationError, NotFoundError
from app.models.membership import Membership
from app.models.organization import Organization

# TODO(M3): class OrganizationService — create (creator=owner), list_for_user,
#           invite_member, change_role, remove_member
