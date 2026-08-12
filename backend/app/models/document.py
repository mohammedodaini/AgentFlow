"""`documents` — knowledge-base file METADATA. Bytes live in object storage.

Layer: models. `status` drives the 202 ingestion flow:
pending → processing → ready | failed.

Why the bytes are not here
--------------------------
Postgres can store a 40 MB PDF in a `bytea` column, and it is the wrong place
for it. Every `SELECT *` drags the blob across the wire, every backup carries
it, every replica copies it, and the row can no longer be read cheaply just to
answer "is this document ready yet?" — which is the single most frequent query
this table will ever serve. The table holds the *facts about* the file; the
file lives behind `app/storage/` (ADR-0007).

`status` is the contract between the API and the worker. The API writes
`pending` and returns 202; the worker moves it to `processing`, then to `ready`
or `failed`. A client polls this column and nothing else.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import BigInteger, Enum, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DocumentStatus(enum.StrEnum):
    """Where a document is in the ingestion pipeline.

    `StrEnum` so the value serialises as `"ready"` rather than
    `"DocumentStatus.READY"` — the same choice `Role` makes in
    `app/models/membership.py`, and for the same reason: these strings appear
    in JSON responses and are part of the public API.
    """

    PENDING = "pending"
    """Row written, bytes stored, job enqueued. Nothing has been parsed."""

    PROCESSING = "processing"
    """A worker has picked it up. Visible so a stuck job is diagnosable."""

    READY = "ready"
    """Ingestion finished. From M6 this also means chunks exist."""

    FAILED = "failed"
    """Ingestion gave up; `error` says why, in words a user can act on."""


class DocumentSource(enum.StrEnum):
    """Where the file came from.

    Only `UPLOAD` is reachable at M5. The others exist because the alternative
    is a nullable column that becomes meaningful later — and because adding a
    value to a native Postgres enum needs a migration, so the cheapest moment
    to declare them is before the type is created at all.
    """

    UPLOAD = "upload"
    GMAIL = "gmail"
    DRIVE = "drive"
    NOTION = "notion"


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One file in an organization's knowledge base."""

    __tablename__ = "documents"

    __table_args__ = (
        # Serves both access patterns from one index: `WHERE organization_id = ?`
        # uses the leading column alone, and the status filter uses both. A
        # separate index on organization_id would be redundant with this one —
        # the mistake M2 caught on `memberships`.
        Index("ix_documents_organization_id_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    """The tenant. Every query in `DocumentRepository` filters on it."""

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    """Who uploaded it — nullable, and `SET NULL` rather than `CASCADE`.

    The document belongs to the organization, not to the person. Someone
    leaving the company must not delete the company's knowledge base, which is
    exactly what `ondelete="CASCADE"` here would do.
    """

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    """Display name — the original filename until someone renames it."""

    source: Mapped[DocumentSource] = mapped_column(
        Enum(
            DocumentSource,
            name="document_source",
            values_callable=lambda enum_class: [member.value for member in enum_class],
        ),
        nullable=False,
        default=DocumentSource.UPLOAD,
    )
    """`values_callable` stores the *values* (`upload`), not the member names
    (`UPLOAD`). Without it Postgres holds the uppercase names and every hand-run
    query has to know that — see `app/models/membership.py`."""

    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    """What the client said it is. Extraction dispatches on this, and a client
    that lies produces `status=failed` rather than a crash."""

    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    """Opaque key into `app/storage/`, not a filesystem path or a URL.

    Opaque on purpose: the local backend and a future GCS backend interpret it
    differently, and nothing outside `app/storage/` is allowed to care.
    """

    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """Size of the stored object.

    Not in the original `docs/database.md` column list; added because the two
    questions it answers — "how much is this tenant using?" and "why is this
    taking so long?" — otherwise require a round trip to object storage per
    row. `BigInteger` because a 32-bit column caps at 2 GB.
    """

    status: Mapped[DocumentStatus] = mapped_column(
        Enum(
            DocumentStatus,
            name="document_status",
            values_callable=lambda enum_class: [member.value for member in enum_class],
        ),
        nullable=False,
        default=DocumentStatus.PENDING,
    )
    """The polled column. See the class docstring for the state machine."""

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Why ingestion failed, phrased for the person who uploaded the file.

    `Text`, not `String(n)`: a truncated error message is worse than no message,
    and in Postgres `text` and `varchar` are the same storage anyway.
    """
