"""Document business logic: the upload→store→202 orchestration.

Layer: services. Owns no transaction and no HTTP concepts; the request edge
commits (`app/db/deps.py`) and the API layer translates errors
(`app/api/errors.py`).

The one thing this service does not do
--------------------------------------
It does not enqueue. That looks like an omission — the whole point of the 202
pattern is that a job gets queued — and it is the most considered decision in
the file.

Enqueueing here would put the job in Redis *inside* the transaction that
created the document row. arq workers are fast; a worker can pick the job up,
query for the document, and find nothing, because the API has not committed
yet. The document is then permanently `pending`, and the only evidence is a
worker log line about a row that plainly exists.

So `upload()` returns the document and its task row, and the caller enqueues
after the commit. `app/api/v1/routes/documents.py` does that with FastAPI's
`BackgroundTasks`, which runs after dependency teardown. The full reasoning,
the alternatives, and the failure that remains, are in ADR-0008.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import NotFoundError, PayloadTooLargeError, UnsupportedMediaTypeError
from app.core.ids import uuid7
from app.models.document import Document, DocumentSource, DocumentStatus
from app.models.task import INGEST_DOCUMENT, Task, TaskStatus
from app.repositories.document_repository import MAX_PAGE_SIZE, DocumentRepository
from app.storage import ObjectStorage, StorageError, build_document_key

logger = structlog.get_logger(__name__)

READ_CHUNK_BYTES = 1024 * 1024
"""How much of an upload to pull off the wire at a time.

Not a performance knob — a safety one. `await upload.read()` with no argument
reads the whole body into memory before anything can object to its size, so a
single request can exhaust the process. Reading in chunks means the size check
runs while the file is still arriving, and the request is refused after one
megabyte rather than after four gigabytes.
"""


class DocumentService:
    """Upload, list, fetch and delete documents for one tenant.

    Takes its collaborators rather than building them: the session comes from
    the request (so the caller owns the transaction), and storage comes from
    `app.state` (so a test passes a temporary directory, and the worker passes
    its own instance). Constructing either here would make every test of this
    class a test of the filesystem as well.
    """

    def __init__(self, session: AsyncSession, storage: ObjectStorage, settings: Settings) -> None:
        self._session = session
        self._storage = storage
        self._settings = settings
        self._documents = DocumentRepository(session)

    # -- commands ---------------------------------------------------------

    async def upload(
        self,
        *,
        organization_id: uuid.UUID,
        uploaded_by: uuid.UUID,
        upload: UploadFile,
    ) -> tuple[Document, Task]:
        """Accept a file: validate, store the bytes, record the intent.

        Returns the document and the task row describing the work still owed.
        Nothing has been parsed when this returns — that is the promise the 202
        makes, and it is why the document comes back `pending`.

        The order of operations is chosen so no failure leaves a lie in the
        database. Validation happens before anything is written. The bytes are
        stored before the row that points at them, so a row never references an
        object that does not exist. And if the database work fails after the
        object landed, the object is removed again — an orphaned file costs
        money and confuses an operator, but it corrupts nothing, which is the
        direction the risk should be taken in.
        """
        content_type = self._validated_content_type(upload)
        data = await self._read_within_limit(upload)

        # Minting the id here rather than letting the column default do it is
        # what makes the storage key computable before the INSERT. The
        # alternative — insert with an empty storage_uri, flush, build the key,
        # update — writes the row twice and leaves a window in which a crash
        # produces a document pointing at nothing.
        document_id = uuid7()
        key = build_document_key(organization_id, document_id, upload.filename)

        storage_uri = await self._storage.put(key, data, content_type=content_type)

        try:
            document = await self._documents.add(
                Document(
                    id=document_id,
                    organization_id=organization_id,
                    uploaded_by=uploaded_by,
                    title=upload.filename or "Untitled",
                    source=DocumentSource.UPLOAD,
                    mime_type=content_type,
                    storage_uri=storage_uri,
                    byte_size=len(data),
                    status=DocumentStatus.PENDING,
                )
            )
            task = await self._add_ingestion_task(document)
        except Exception:
            # A compensating action, not error handling: the exception is
            # re-raised. Without this the bytes stay behind forever, because
            # nothing in the database will ever again mention their key.
            await self._delete_quietly(storage_uri)
            raise

        logger.info(
            "document.uploaded",
            document_id=str(document.id),
            organization_id=str(organization_id),
            mime_type=content_type,
            bytes=len(data),
        )
        return document, task

    async def delete(self, organization_id: uuid.UUID, document_id: uuid.UUID) -> str:
        """Delete a document row and return the storage key of its bytes.

        The bytes are *not* deleted here. The caller removes them after the
        transaction commits — because deleting them first and then failing to
        commit leaves a row pointing at an object that no longer exists, which
        is a broken invariant rather than a recoverable leak.

        Returning a bare `str` for the caller to act on is a small ugliness
        with a clear justification: the alternative is passing FastAPI's
        `BackgroundTasks` into a service that arq workers also call, and a
        worker has no such object.
        """
        document = await self.get(organization_id, document_id)
        storage_uri = document.storage_uri

        await self._documents.delete(document)

        logger.info("document.deleted", document_id=str(document_id))
        return storage_uri

    # -- queries ----------------------------------------------------------

    async def get(self, organization_id: uuid.UUID, document_id: uuid.UUID) -> Document:
        """One document, or `NotFoundError`.

        A document belonging to another organization raises the same error as
        one that never existed. Distinguishing them would turn this endpoint
        into an oracle for enumerating document ids across tenants — the rule
        `OrganizationService.get_membership` already follows.
        """
        document = await self._documents.get(organization_id, document_id)

        if document is None:
            message = "Document not found"
            raise NotFoundError(message)

        return document

    async def list(
        self,
        organization_id: uuid.UUID,
        *,
        status: DocumentStatus | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Document], int]:
        """A page of documents plus the total, for the `Page` envelope.

        `limit` is clamped rather than rejected. A client asking for 5,000 rows
        is not attacking anything — it is guessing — and answering 422 teaches
        it nothing that returning 100 rows with an honest `total` does not.
        """
        capped = max(1, min(limit, MAX_PAGE_SIZE))
        offset = max(0, offset)

        documents = await self._documents.list_for_organization(
            organization_id, status=status, limit=capped, offset=offset
        )
        total = await self._documents.count_for_organization(organization_id, status=status)
        return documents, total

    # -- internals --------------------------------------------------------

    def _validated_content_type(self, upload: UploadFile) -> str:
        """Check the declared type against the allowlist, before reading a byte.

        Before, because refusing a 25 MB video after buffering it is a refusal
        that already cost what it was meant to save.

        This trusts the client's `Content-Type`. That is a real limitation: a
        renamed executable announced as `application/pdf` gets stored. It is
        bounded rather than dangerous — nothing executes the file, extraction
        fails, and the document ends `failed` — but content sniffing (magic
        bytes) belongs in the M16 hardening pass, and pretending otherwise here
        would be worse than saying so.
        """
        content_type = (upload.content_type or "").split(";")[0].strip().lower()

        if content_type not in self._settings.allowed_upload_mime_types:
            allowed = ", ".join(sorted(self._settings.allowed_upload_mime_types))
            message = f"Cannot accept {content_type or 'an unknown type'}. Supported: {allowed}."
            raise UnsupportedMediaTypeError(message)

        return content_type

    async def _read_within_limit(self, upload: UploadFile) -> bytes:
        """Read the upload, refusing it the moment it exceeds the limit.

        The check is inside the loop on purpose. Reading everything and then
        measuring it is the same as having no limit: by the time the comparison
        runs the memory has already been spent, which is precisely what an
        attacker sending a 4 GB body is trying to make happen.
        """
        limit = self._settings.max_upload_bytes
        chunks: list[bytes] = []
        total = 0

        while chunk := await upload.read(READ_CHUNK_BYTES):
            total += len(chunk)

            if total > limit:
                megabytes = limit / (1024 * 1024)
                message = f"File is larger than the {megabytes:.0f} MB limit."
                raise PayloadTooLargeError(message)

            chunks.append(chunk)

        return b"".join(chunks)

    async def _add_ingestion_task(self, document: Document) -> Task:
        """Record, in Postgres, that ingestion is owed for this document.

        Written in the same transaction as the document, which is what makes
        "a document exists but nothing remembers to process it" impossible at
        the database level. Redis can lose the job; this row is the evidence
        that lets a sweeper re-enqueue it.
        """
        task = Task(
            organization_id=document.organization_id,
            kind=INGEST_DOCUMENT,
            payload={
                "document_id": str(document.id),
                "organization_id": str(document.organization_id),
            },
            status=TaskStatus.QUEUED,
        )
        self._session.add(task)
        await self._session.flush()
        return task

    async def _delete_quietly(self, storage_uri: str) -> None:
        """Best-effort cleanup on a path that is already failing.

        Swallows storage errors deliberately. This runs inside an `except`
        block that is about to re-raise the *real* error, and letting a cleanup
        failure replace it would hide the cause behind a symptom — the classic
        way a stack trace ends up describing the wrong problem entirely.
        """
        try:
            await self._storage.delete(storage_uri)
        except StorageError:
            logger.exception("document.orphaned_object", storage_uri=storage_uri)
