# ruff: noqa: F401  — remove once this module is implemented (M3)
"""Organization + membership API shapes."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from app.schemas.common import APIModel

# TODO(M3): OrganizationCreate — name, slug
# TODO(M3): OrganizationRead — id, name, slug, plan
# TODO(M3): MemberRead — user_id, email, full_name, role
# TODO(M3): MemberInvite — email, role
