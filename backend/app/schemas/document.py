"""Document API shapes (M5). Search and ask land at M6/M7.

Layer: schemas — the API boundary. See `app/schemas/common.py` for why an ORM
object must never cross it.

That rule earns its keep here more than anywhere so far. `Document` carries
`storage_uri`, and returning the model directly would publish the internal key
layout of object storage — telling a client exactly what to guess at if a
bucket is ever misconfigured, and freezing a private naming scheme into the
public API where it can never be changed. `DocumentRead` is a whitelist, so
that column simply is not in the response.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.models.document import DocumentSource, DocumentStatus
from app.schemas.common import APIModel


class DocumentRead(APIModel):
    """One document, as a client sees it.

    The status fields are the reason this shape exists at all: upload answers
    202 with `status="pending"`, and the client polls this same schema until it
    reads `ready` or `failed`. One representation for the just-accepted
    document and the finished one means the client parses one thing, not two.

    Importing `DocumentStatus` from the model looks like a layering violation
    and is not: the enum is part of the *contract*, not part of the table. If
    the schema declared its own copy, the two would drift, and the day someone
    added `quarantined` to the model the API would start raising validation
    errors on perfectly valid rows.
    """

    id: uuid.UUID
    title: str = Field(description="Display name — the original filename until renamed")
    source: DocumentSource
    mime_type: str
    byte_size: int = Field(description="Size of the stored file in bytes")
    status: DocumentStatus = Field(description="pending → processing → ready | failed")

    error: str | None = Field(
        default=None,
        description="Why ingestion failed. Non-null only when status is 'failed'.",
    )
    """Returned rather than buried in a log line, because the person who can fix
    it — re-export the PDF, add a text layer — is the person holding the file,
    and they cannot read our logs."""

    uploaded_by: uuid.UUID | None = Field(
        default=None, description="User who uploaded it; null if that account was deleted"
    )

    created_at: datetime
    updated_at: datetime
    """`updated_at` is how a polling client knows something moved. Without it a
    document stuck in `processing` looks identical whether the worker is
    grinding through page 300 or died an hour ago."""
