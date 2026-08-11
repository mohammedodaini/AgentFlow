# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M3)
"""/organizations — org CRUD + member management (invite, change role, remove)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.schemas.organization import MemberRead, OrganizationCreate, OrganizationRead
from app.services.organization_service import OrganizationService

router = APIRouter(prefix="/organizations", tags=["organizations"])

# TODO(M3): POST / — create org (creator becomes owner)
# TODO(M3): GET / — orgs the current user belongs to
# TODO(M3): GET /{org_id}/members · POST /{org_id}/members (invite) —
#           role checks: only owner|admin manage members
