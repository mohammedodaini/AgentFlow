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

from pydantic import BaseModel, Field

from app.models.document import DocumentSource, DocumentStatus
from app.repositories.chunk_repository import MAX_TOP_K
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


class SearchRequest(BaseModel):
    """A retrieval query (M6).

    A POST body rather than a query string, because a question is user text of
    arbitrary length and shape. Query strings have practical length limits, land
    verbatim in access logs and browser history, and require escaping that
    everyone eventually gets wrong. The rule this follows: parameters describe
    *how* to search, content describes *what* to search for.
    """

    query: str = Field(min_length=1, max_length=2000, description="Natural-language question")
    """Bounded at both ends. Empty is meaningless — an embedding of nothing
    matches arbitrarily — and unbounded input is billable text someone else
    chose the size of."""

    top_k: int = Field(default=5, ge=1, le=MAX_TOP_K, description="How many chunks to return")
    """Validated against the repository's own ceiling rather than a second
    literal, so the two cannot disagree."""


class SearchResult(APIModel):
    """One retrieved chunk, with everything needed to cite it.

    `document_id` and `document_title` travel with the chunk deliberately. A
    result the user cannot trace back to a source is an assertion, not
    evidence — and at M7, when a model writes prose over these chunks, the
    citation is the only thing standing between a summary and a plausible
    invention.
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    chunk_index: int = Field(description="Position within the document, from 0")
    content: str
    score: float = Field(ge=0.0, le=1.0, description="Cosine similarity; 1.0 is identical")
    """Returned rather than hidden. A client that shows "62% match" lets a user
    judge a weak answer for themselves, which is far better than a system that
    silently drops it or presents it with the same confidence as a direct hit."""
