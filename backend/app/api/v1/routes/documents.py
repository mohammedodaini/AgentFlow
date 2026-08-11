# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M5)
"""/documents — upload + status. THE 202 pattern lives here (quiz Q2).

Upload stores bytes + a pending `documents` row, enqueues ingestion via arq,
and returns 202 immediately — parsing a 40-page PDF must not block HTTP.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, UploadFile

from app.auth.dependencies import get_current_user
from app.schemas.document import DocumentRead
from app.services.document_service import DocumentService

router = APIRouter(prefix="/documents", tags=["documents"])

# TODO(M5): POST / — accept UploadFile, return 202 + DocumentRead (status=pending)
# TODO(M5): GET / — list org documents · GET /{id} — poll ingestion status
# TODO(M5): DELETE /{id} — cascades to chunks
