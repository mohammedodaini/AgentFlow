# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M5)
"""Document queries. Repository justified: status filters, joins to chunks.

Every method takes organization_id — tenancy scoping is not optional.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document

# TODO(M5): class DocumentRepository — get(org_id, id), list_by_org(org_id, status=None),
#           set_status(id, status, error=None)
