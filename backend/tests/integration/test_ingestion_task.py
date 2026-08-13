"""The ingestion worker task (M5).

Called directly rather than through a running arq worker. That is deliberate:
booting arq would test arq, take seconds, and need a live Redis — while the
behaviour worth pinning is entirely in this function. What it does with a
corrupt PDF, a deleted document, a second delivery of the same job, and a
failure on the last retry are all decisions made here.

`ctx` is built by hand for the same reason — see `tests/worker_harness.py`,
which does it, and which the end-to-end search test shares.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentStatus, TaskStatus
from app.models.document_chunk import EMBEDDING_DIMENSIONS
from app.models.task import INGEST_DOCUMENT, Task
from app.repositories.chunk_repository import ChunkRepository
from app.storage import ObjectStorage, build_document_key
from app.workers.tasks import ingestion
from app.workers.tasks.ingestion import ingest_document
from tests.factories import TEXT_BYTES, make_document, make_org_with_owner, make_pdf
from tests.worker_harness import run, worker_context


async def stored_document(
    session: AsyncSession, storage: ObjectStorage, data: bytes, mime_type: str
) -> tuple[Document, Task]:
    """A document whose bytes really exist in storage, plus its task row."""
    organization, user, _ = await make_org_with_owner(session)
    document = await make_document(
        session, organization=organization, uploaded_by=user, mime_type=mime_type
    )

    key = build_document_key(organization.id, document.id, document.title)
    document.storage_uri = await storage.put(key, data, content_type=mime_type)

    task = Task(
        organization_id=organization.id,
        kind=INGEST_DOCUMENT,
        payload={"document_id": str(document.id)},
        status=TaskStatus.QUEUED,
    )
    session.add(task)
    await session.flush()
    return document, task


# --------------------------------------------------------------------------
# the happy paths
# --------------------------------------------------------------------------


async def test_a_text_document_becomes_ready(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    document, task = await stored_document(db_session, storage, TEXT_BYTES, "text/plain")

    result = await run(worker_context(db_session, storage), document, task)

    assert document.status is DocumentStatus.READY
    assert document.error is None
    assert task.status is TaskStatus.SUCCEEDED
    assert task.attempts == 1
    assert result["characters"] == len(TEXT_BYTES.decode())
    assert task.result == result, "Postgres and the arq result must not disagree"


async def test_a_pdf_document_becomes_ready(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    document, task = await stored_document(
        db_session, storage, make_pdf("Quarterly report"), "application/pdf"
    )

    await run(worker_context(db_session, storage), document, task)

    assert document.status is DocumentStatus.READY


# --------------------------------------------------------------------------
# indexing (M6)
# --------------------------------------------------------------------------


async def test_a_ready_document_is_actually_indexed(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """`ready` has to mean *searchable*, not merely *parsed*.

    At M5 the two were the same thing. From M6 they can diverge, and the
    divergence is silent: a document whose text extracted fine but whose chunks
    were never written reads `ready` in the API, appears in every listing, and
    answers no question ever asked of it.
    """
    document, task = await stored_document(db_session, storage, TEXT_BYTES, "text/plain")

    result = await run(worker_context(db_session, storage), document, task)

    assert document.status is DocumentStatus.READY
    assert result["chunks"] >= 1
    assert await ChunkRepository(db_session).count_for_document(document.id) == result["chunks"]


async def test_re_indexing_a_document_does_not_duplicate_its_chunks(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The `already_ready` guard is the first line of defence and not the only
    one it should have.

    A retry that gets past the guard — a document reset to `pending` by an
    operator, a sweeper re-enqueueing a stuck job — must still replace the
    index rather than append to it. Duplicated chunks do not announce
    themselves; they quietly take two of the top five slots in every search
    that matches them.
    """
    document, task = await stored_document(db_session, storage, TEXT_BYTES, "text/plain")
    await run(worker_context(db_session, storage), document, task)
    first = await ChunkRepository(db_session).count_for_document(document.id)

    document.status = DocumentStatus.PENDING
    await db_session.flush()
    await run(worker_context(db_session, storage), document, task)

    assert await ChunkRepository(db_session).count_for_document(document.id) == first


async def test_a_vector_of_the_wrong_width_fails_the_document_with_a_readable_reason(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The misconfiguration this check exists to name.

    Left to Postgres, a provider returning 3072 dimensions into a
    `vector(1536)` column raises a `DataError` about the column — several
    layers below the actual mistake, which is one environment variable. The
    message here names the two settings a human would have to change.
    """
    document, task = await stored_document(db_session, storage, TEXT_BYTES, "text/plain")

    result = await run(
        worker_context(db_session, storage, embedder=WrongWidthEmbedder()), document, task
    )

    assert document.status is DocumentStatus.FAILED
    assert "EMBEDDING_DIMENSIONS" in (document.error or "")
    assert task.status is TaskStatus.FAILED
    assert result["error"] == document.error


async def test_an_embedder_returning_too_few_vectors_fails_the_document(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The check that stops a mismatch becoming silent data corruption.

    `zip(chunks, vectors, strict=True)` would raise on a length mismatch, but
    only after the failure had travelled far enough that the message names
    `zip`. Worse, a *non-strict* zip — the natural thing to write — would
    truncate: the tail of the document would simply never be indexed, the
    document would go `ready`, and nothing anywhere would say so.
    """
    document, task = await stored_document(db_session, storage, TEXT_BYTES, "text/plain")

    result = await run(
        worker_context(db_session, storage, embedder=ShortCountEmbedder()), document, task
    )

    assert document.status is DocumentStatus.FAILED
    assert "vectors" in (document.error or "")
    assert result["error"] == document.error


class ShortCountEmbedder:
    """Returns one fewer vector than it was asked for."""

    dimensions = EMBEDDING_DIMENSIONS

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * self.dimensions for _ in texts[:-1]]

    async def embed_query(self, text: str) -> list[float]:
        del text
        return [0.1] * self.dimensions


class WrongWidthEmbedder:
    """A provider configured for a different model than the column was built for.

    Not a mock of the seam for convenience — it is the one input the offline
    embedder cannot produce, because it is correct by construction.
    """

    dimensions = 8

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * self.dimensions for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        del text
        return [0.1] * self.dimensions


async def test_text_that_chunks_to_nothing_fails_rather_than_indexing_nothing(
    db_session: AsyncSession, storage: ObjectStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A document with no chunks is not `ready`, whatever else is true of it.

    Extraction already refuses empty documents, so reaching this state means
    text survived that check and still produced nothing indexable. Marking it
    `ready` would put a permanently unfindable document in the user's library
    with no indication that anything went wrong — the exact failure the status
    field exists to prevent.
    """
    document, task = await stored_document(db_session, storage, TEXT_BYTES, "text/plain")
    monkeypatch.setattr(ingestion, "chunk_text", lambda *_args, **_kwargs: [])

    result = await run(worker_context(db_session, storage), document, task)

    assert document.status is DocumentStatus.FAILED
    assert "no indexable text" in (document.error or "")
    assert result["error"] == document.error


# --------------------------------------------------------------------------
# permanent failures: recorded, never retried
# --------------------------------------------------------------------------


async def test_a_corrupt_pdf_fails_with_a_message_the_user_can_act_on(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The job returns normally rather than raising.

    Raising would let arq retry, and a corrupt PDF fails identically all three
    times — burning a worker slot and delaying every job behind it to learn
    nothing.
    """
    document, task = await stored_document(db_session, storage, make_pdf()[:100], "application/pdf")

    result = await run(worker_context(db_session, storage), document, task)

    assert document.status is DocumentStatus.FAILED
    assert "corrupt or truncated" in (document.error or "")
    assert task.status is TaskStatus.FAILED
    assert result["error"] == document.error


async def test_missing_bytes_fail_the_document_rather_than_crashing(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """A row pointing at an object that is gone. Rare, and it must produce a
    diagnosable `failed` document rather than an unhandled exception."""
    organization, user, _ = await make_org_with_owner(db_session)
    document = await make_document(db_session, organization=organization, uploaded_by=user)
    task = Task(
        organization_id=organization.id,
        kind=INGEST_DOCUMENT,
        payload={"document_id": str(document.id)},
        status=TaskStatus.QUEUED,
    )
    db_session.add(task)
    await db_session.flush()

    await run(worker_context(db_session, storage), document, task)

    assert document.status is DocumentStatus.FAILED
    assert task.status is TaskStatus.FAILED


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------


async def test_a_second_delivery_of_the_same_job_does_no_work(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """arq retries and a sweeper may re-enqueue, so double delivery is normal.

    At M5 re-parsing would merely be wasteful. From M6 it would duplicate every
    chunk of the document, so the guard is established now rather than after it
    has become a data-corruption bug.
    """
    document, task = await stored_document(db_session, storage, TEXT_BYTES, "text/plain")
    await run(worker_context(db_session, storage), document, task)

    second = await run(worker_context(db_session, storage), document, task)

    assert second == {"skipped": "already_ready"}
    assert document.status is DocumentStatus.READY


async def test_a_document_deleted_before_the_worker_ran_is_not_an_error(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The user changed their mind between enqueue and dequeue. The correct
    response is to stop, not to retry work nobody wants."""
    document, task = await stored_document(db_session, storage, TEXT_BYTES, "text/plain")
    document_id, organization_id = document.id, document.organization_id
    await db_session.delete(document)
    await db_session.flush()

    result = await ingest_document(
        worker_context(db_session, storage),
        document_id=str(document_id),
        organization_id=str(organization_id),
        task_id=str(task.id),
    )

    assert result == {"skipped": "deleted"}
    assert task.status is TaskStatus.SUCCEEDED


# --------------------------------------------------------------------------
# transient failures: retried, and recorded on the last attempt
# --------------------------------------------------------------------------


async def test_an_unexpected_error_propagates_so_arq_can_retry(
    db_session: AsyncSession, storage: ObjectStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Postgres failing over is worth retrying. The document stays
    `processing`, which is honest — something *is* still going to try."""
    document, task = await stored_document(db_session, storage, TEXT_BYTES, "text/plain")

    async def explode(*_args: object, **_kwargs: object) -> str:
        message = "connection reset"
        raise RuntimeError(message)

    monkeypatch.setattr(ingestion, "_extract", explode)

    with pytest.raises(RuntimeError):
        await run(worker_context(db_session, storage, job_try=1, max_tries=3), document, task)

    assert document.status is DocumentStatus.PROCESSING


async def test_the_last_attempt_records_the_failure_before_giving_up(
    db_session: AsyncSession, storage: ObjectStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hole this closes.

    Without it the retry policy is invisible in exactly the wrong way: the
    first two failures are unrecorded *and correct* (something will try again),
    and the third is unrecorded and wrong (nothing will). The user sees
    `processing` in both cases and can never tell them apart.
    """
    document, task = await stored_document(db_session, storage, TEXT_BYTES, "text/plain")

    async def explode(*_args: object, **_kwargs: object) -> str:
        message = "still broken"
        raise RuntimeError(message)

    monkeypatch.setattr(ingestion, "_extract", explode)

    with pytest.raises(RuntimeError):
        await run(worker_context(db_session, storage, job_try=3, max_tries=3), document, task)

    assert document.status is DocumentStatus.FAILED
    assert "again" in (document.error or ""), "the message must tell the user what to do"
    assert task.status is TaskStatus.FAILED
    assert task.result is not None
    assert task.result["attempts"] == 3


async def test_a_missing_task_row_does_not_stop_the_real_work(
    db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """Bookkeeping must not outrank the job. Refusing to ingest a document
    because its mirror row was deleted would be the tail wagging the dog."""
    document, task = await stored_document(db_session, storage, TEXT_BYTES, "text/plain")
    await db_session.delete(task)
    await db_session.flush()

    result = await ingest_document(
        worker_context(db_session, storage),
        document_id=str(document.id),
        organization_id=str(document.organization_id),
        task_id=str(uuid.uuid4()),
    )

    assert document.status is DocumentStatus.READY
    assert "characters" in result
