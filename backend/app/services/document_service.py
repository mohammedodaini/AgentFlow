# ruff: noqa: F401  — remove once this module is implemented (M5)
"""Document business logic: the upload→enqueue→202 orchestration (quiz Q2).

upload(): store bytes in object storage, insert pending row, enqueue arq job,
return immediately. The WORKER (workers/tasks/ingestion.py) does the heavy
lifting by calling app/rag/ingestion.py.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DocumentIngestionError, NotFoundError
from app.repositories.document_repository import DocumentRepository

# TODO(M5): class DocumentService — upload(org_id, user_id, file), get_status,
#           list(org_id), delete (storage object + row; chunks cascade)
