# ruff: noqa: F401  — remove once this module is implemented (M5)
"""`documents` — knowledge-base file METADATA. Bytes live in object storage.

status drives the 202 ingestion flow: pending → processing → ready | failed.
"""

from __future__ import annotations

import enum

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

# TODO(M5): class DocumentStatus(enum.StrEnum) — pending|processing|ready|failed
# TODO(M5): class Document(Base) — organization_id FK, uploaded_by FK users, title,
#           source (upload|gmail|drive|notion), mime_type, storage_uri, status, error;
#           relationship chunks -> DocumentChunk (cascade delete)
