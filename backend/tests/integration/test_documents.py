"""DocumentService and DocumentRepository against a real database (M5).

Integration rather than unit, because the behaviours worth pinning here are
enforced by things a mock cannot reproduce: a tenancy filter is a `WHERE`
clause, a page boundary is an `ORDER BY` plus `LIMIT`, and a cleanup path is
only interesting if the write it compensates for really happened.

Storage is real too — a `LocalObjectStorage` rooted in the test's `tmp_path`.
See `tests/unit/test_storage.py` for why it is not mocked.
"""

from __future__ import annotations

import io
import pathlib

import pytest
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers

from app.core.config import Settings, get_settings
from app.core.exceptions import NotFoundError, PayloadTooLargeError, UnsupportedMediaTypeError
from app.models import DocumentStatus, TaskStatus
from app.models.task import INGEST_DOCUMENT
from app.repositories.document_repository import DocumentRepository
from app.services.document_service import DocumentService
from app.storage import ObjectNotFoundError, ObjectStorage, StorageError
from tests.factories import TEXT_BYTES, make_document, make_org_with_owner, make_pdf


def upload_file(
    data: bytes, *, filename: str = "handbook.pdf", content_type: str = "application/pdf"
) -> UploadFile:
    """Build the same object FastAPI hands a route for a multipart file.

    Constructed by hand rather than driven over HTTP so these tests can reach
    the service directly. `content_type` goes in the headers because that is
    where `UploadFile.content_type` reads it from — setting an attribute would
    produce an object that behaves differently from the real one.
    """
    return UploadFile(
        file=io.BytesIO(data),
        filename=filename,
        size=len(data),
        headers=Headers({"content-type": content_type}),
    )


def service(
    session: AsyncSession, storage: ObjectStorage, settings: Settings | None = None
) -> DocumentService:
    return DocumentService(session, storage, settings or get_settings())


def stored_files() -> list[pathlib.Path]:
    """Every object currently on disk, read from configuration rather than
    from the storage object — the protocol has no `root`, and reaching for one
    would be exactly the leak the protocol exists to prevent."""
    root = pathlib.Path(get_settings().storage_local_path)
    return [path for path in root.rglob("*") if path.is_file()] if root.exists() else []


# --------------------------------------------------------------------------
# upload
# --------------------------------------------------------------------------


async def test_upload_stores_the_bytes_and_records_pending_work(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The whole 202 contract in one assertion block.

    Note what is *not* true afterwards: nothing has been parsed, and the
    document is `pending`. Claiming otherwise is what a 201 would do.
    """
    organization, user, _ = await make_org_with_owner(db_session)
    pdf = make_pdf()

    document, task = await service(db_session, storage).upload(
        organization_id=organization.id, uploaded_by=user.id, upload=upload_file(pdf)
    )

    assert document.status is DocumentStatus.PENDING
    assert document.byte_size == len(pdf)
    assert document.title == "handbook.pdf"
    assert await storage.get(document.storage_uri) == pdf, "the bytes must be retrievable"

    assert task.kind == INGEST_DOCUMENT
    assert task.status is TaskStatus.QUEUED
    assert task.payload["document_id"] == str(document.id)
    assert task.attempts == 0


async def test_the_storage_key_is_scoped_to_the_tenant(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """Tenant-first keys are what make "erase this customer" a prefix delete."""
    organization, user, _ = await make_org_with_owner(db_session)

    document, _ = await service(db_session, storage).upload(
        organization_id=organization.id, uploaded_by=user.id, upload=upload_file(make_pdf())
    )

    assert document.storage_uri.startswith(f"organizations/{organization.id}/")


async def test_a_dangerous_filename_cannot_escape_the_storage_root(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """End-to-end proof of the sanitiser, not just a unit test of it."""
    organization, user, _ = await make_org_with_owner(db_session)

    document, _ = await service(db_session, storage).upload(
        organization_id=organization.id,
        uploaded_by=user.id,
        upload=upload_file(TEXT_BYTES, filename="../../../etc/passwd", content_type="text/plain"),
    )

    assert ".." not in document.storage_uri
    assert document.storage_uri.endswith("/etc_passwd")
    assert document.title == "../../../etc/passwd", "the display name is kept verbatim"


async def test_an_unsupported_type_is_refused_before_anything_is_written(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """Refusing after buffering a 25 MB file already spent what the limit was
    meant to save. Nothing may exist afterwards — no object, no row."""
    organization, user, _ = await make_org_with_owner(db_session)

    with pytest.raises(UnsupportedMediaTypeError) as error:
        await service(db_session, storage).upload(
            organization_id=organization.id,
            uploaded_by=user.id,
            upload=upload_file(b"MZ\x90\x00", content_type="application/x-msdownload"),
        )

    assert "application/pdf" in str(error.value), "the message must list what does work"
    assert await DocumentRepository(db_session).count_for_organization(organization.id) == 0
    assert stored_files() == []


async def test_a_file_over_the_limit_is_refused(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The limit is enforced while reading, so this fails without the whole
    file ever being held in memory at once."""
    organization, user, _ = await make_org_with_owner(db_session)
    tiny_limit = get_settings().model_copy(update={"max_upload_bytes": 8})

    with pytest.raises(PayloadTooLargeError):
        await service(db_session, storage, tiny_limit).upload(
            organization_id=organization.id,
            uploaded_by=user.id,
            upload=upload_file(TEXT_BYTES, content_type="text/plain"),
        )


async def test_the_object_is_removed_when_the_row_cannot_be_written(
    db_session: AsyncSession, storage: ObjectStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compensating action.

    Bytes are stored before the row that points at them, so a database failure
    after the write leaves an object nothing will ever mention again. Without
    the cleanup those bytes are billed forever and are indistinguishable from
    real data.
    """
    organization, user, _ = await make_org_with_owner(db_session)

    async def explode(self: DocumentRepository, document: object) -> None:
        message = "database went away"
        raise RuntimeError(message)

    monkeypatch.setattr(DocumentRepository, "add", explode)

    with pytest.raises(RuntimeError):
        await service(db_session, storage).upload(
            organization_id=organization.id, uploaded_by=user.id, upload=upload_file(make_pdf())
        )

    assert stored_files() == [], "the orphaned object should have been cleaned up"


async def test_a_failing_cleanup_does_not_hide_the_real_error(
    db_session: AsyncSession, storage: ObjectStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compensating action must never replace the exception it compensates.

    If storage is also broken — which is entirely plausible, since a failing
    upload and a failing delete have overlapping causes — a cleanup that raised
    would surface as `StorageError` and bury the database failure that actually
    started it. That is the classic way a stack trace ends up describing the
    wrong problem.
    """
    organization, user, _ = await make_org_with_owner(db_session)

    async def explode_db(self: DocumentRepository, document: object) -> None:
        message = "the real problem"
        raise RuntimeError(message)

    async def explode_storage(*_args: object, **_kwargs: object) -> None:
        message = "cleanup also broken"
        raise StorageError(message)

    monkeypatch.setattr(DocumentRepository, "add", explode_db)
    monkeypatch.setattr(type(storage), "delete", explode_storage)

    with pytest.raises(RuntimeError, match="the real problem"):
        await service(db_session, storage).upload(
            organization_id=organization.id, uploaded_by=user.id, upload=upload_file(make_pdf())
        )


# --------------------------------------------------------------------------
# reads, and the tenancy boundary
# --------------------------------------------------------------------------


async def test_a_document_from_another_organization_is_not_found(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """404, not 403, and the same error as a document that never existed.

    Distinguishing the two would turn this into an oracle for enumerating
    document ids across tenants — the rule `OrganizationService` already keeps.
    """
    mine, _, _ = await make_org_with_owner(db_session)
    theirs, _, _ = await make_org_with_owner(db_session)
    document = await make_document(db_session, organization=theirs)

    with pytest.raises(NotFoundError):
        await service(db_session, storage).get(mine.id, document.id)


async def test_listing_is_scoped_filtered_and_newest_first(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """Three properties at once, because they are one query.

    Ordering is on the UUIDv7 primary key, descending. The assertion is written
    as "descending by id" rather than "in reverse creation order" on purpose:
    all three rows here are created inside the same millisecond, and our
    `uuid7()` is the pure-random variant of RFC 9562 §5.7 with no monotonic
    counter, so sub-millisecond order is decided by random bits.

    That is a real limitation and it is the *right* one to have. What
    pagination needs is a total, stable order, which this is; what it cannot
    survive is ties, which ordering by `created_at` would produce in bulk —
    `now()` is transaction start time in PostgreSQL, so every row written by
    one request shares it exactly.
    """
    organization, _, _ = await make_org_with_owner(db_session)
    other, _, _ = await make_org_with_owner(db_session)

    mine = [
        await make_document(db_session, organization=organization, title="first.pdf"),
        await make_document(db_session, organization=organization, title="second.pdf"),
    ]
    ready = await make_document(
        db_session, organization=organization, title="done.pdf", status=DocumentStatus.READY
    )
    mine.append(ready)
    await make_document(db_session, organization=other, title="not-mine.pdf")

    documents, total = await service(db_session, storage).list(organization.id)

    assert total == 3, "the other tenant's document must not be counted"
    assert [document.id for document in documents] == sorted(
        (document.id for document in mine), reverse=True
    )

    only_ready, ready_total = await service(db_session, storage).list(
        organization.id, status=DocumentStatus.READY
    )
    assert ready_total == 1
    assert [document.id for document in only_ready] == [ready.id]


async def test_an_absurd_page_size_is_clamped_not_rejected(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """A client asking for 5,000 rows is guessing, not attacking. Answering 422
    teaches it nothing that a capped page with an honest total does not."""
    organization, _, _ = await make_org_with_owner(db_session)
    await make_document(db_session, organization=organization)

    documents, total = await service(db_session, storage).list(organization.id, limit=100_000)

    assert total == 1
    assert len(documents) == 1


# --------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------


async def test_delete_removes_the_row_and_hands_back_the_storage_key(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The service deliberately does not delete the bytes.

    Deleting them first and then failing to commit would leave a row pointing
    at an object that no longer exists — a broken invariant. Deleting the row
    first risks an orphaned file, which costs money and breaks nothing. The
    route removes the bytes after the commit.
    """
    organization, user, _ = await make_org_with_owner(db_session)
    document, _ = await service(db_session, storage).upload(
        organization_id=organization.id, uploaded_by=user.id, upload=upload_file(make_pdf())
    )

    storage_uri = await service(db_session, storage).delete(organization.id, document.id)

    assert storage_uri == document.storage_uri
    assert await DocumentRepository(db_session).get(organization.id, document.id) is None
    assert await storage.get(storage_uri), "bytes survive until the caller removes them"

    await storage.delete(storage_uri)
    with pytest.raises(ObjectNotFoundError):
        await storage.get(storage_uri)


async def test_deleting_another_organizations_document_is_a_404(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    mine, _, _ = await make_org_with_owner(db_session)
    theirs, _, _ = await make_org_with_owner(db_session)
    document = await make_document(db_session, organization=theirs)

    with pytest.raises(NotFoundError):
        await service(db_session, storage).delete(mine.id, document.id)
