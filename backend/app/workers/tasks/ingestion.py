"""arq task: `ingest_document` — the slow half of the 202 pattern.

Layer: workers. Runs in the worker process, not the API process: no request, no
client waiting, no HTTP status to return. Everything it has to say, it says by
writing to `documents.status` / `documents.error`, which the API serves back
when the client polls.

Two properties this function must have, and how each is achieved
----------------------------------------------------------------
**Idempotent.** arq retries, and a sweeper may re-enqueue. Running twice must
not produce two of anything. A document already `ready` returns early instead
of redoing the work, and — since M6, when this task began inserting chunks —
indexing *replaces* a document's chunks rather than appending to them. That
second guarantee is the one that matters now: appending on a retry would double
every chunk, and duplicates do not announce themselves, they just quietly take
two of the top five slots in every search that matches them.

**Honest about which failures are worth retrying.** A corrupt PDF fails
identically on all three attempts; retrying it wastes a worker slot and delays
every job behind it. So a `DocumentIngestionError` ends the job — the document
is marked `failed` and the function returns normally. Anything unexpected —
Postgres restarting, Redis blipping — propagates, and arq retries it, which is
what retries are actually for.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.exceptions import DocumentIngestionError
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import EMBEDDING_DIMENSIONS, DocumentChunk
from app.models.task import Task, TaskStatus
from app.rag.chunking import chunk_text
from app.rag.embeddings import EmbeddingProvider
from app.rag.ingestion import extract_text
from app.repositories.chunk_repository import ChunkRepository
from app.repositories.document_repository import DocumentRepository
from app.storage import ObjectStorage, StorageError

logger = structlog.get_logger(__name__)


async def ingest_document(
    ctx: dict[str, Any],
    *,
    document_id: str,
    organization_id: str,
    task_id: str,
) -> dict[str, Any]:
    """Parse a stored document and record the outcome.

    `ctx` is arq's per-worker context, populated by `WorkerSettings.on_startup`.
    The worker is a separate process from the API, so it holds its own session
    factory and its own storage client — it cannot borrow `app.state`, and a
    module-level engine shared "just for the worker" is how two processes end
    up sharing one connection pool that belongs to neither.

    Ids arrive as strings because the queue payload is serialised; they are
    parsed back here rather than trusted, so a malformed job fails loudly at the
    boundary instead of deep inside a query.
    """
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    storage: ObjectStorage = ctx["storage"]

    document_uuid = uuid.UUID(document_id)
    organization_uuid = uuid.UUID(organization_id)
    task_uuid = uuid.UUID(task_id)

    log = logger.bind(document_id=document_id, task_id=task_id, job_try=ctx.get("job_try", 1))

    async with session_factory() as session:
        task = await _start_task(session, task_uuid)
        documents = DocumentRepository(session)
        document = await documents.get(organization_uuid, document_uuid)

        if document is None:
            # Deleted between enqueue and dequeue. Not an error: the user
            # changed their mind, and the correct response is to stop. Retrying
            # would burn all three attempts on work nobody wants.
            log.info("ingestion.document_gone")
            return await _finish(session, task, TaskStatus.SUCCEEDED, {"skipped": "deleted"})

        if document.status is DocumentStatus.READY:
            # The idempotency guard. A duplicate delivery must not re-parse a
            # finished document, and — from M6 — must not duplicate its chunks.
            log.info("ingestion.already_ready")
            return await _finish(session, task, TaskStatus.SUCCEEDED, {"skipped": "already_ready"})

        await documents.set_status(document, DocumentStatus.PROCESSING)
        await session.commit()

        try:
            text = await _extract(storage, document)
        except (DocumentIngestionError, StorageError) as error:
            # A permanent failure. The message is written for the person who
            # uploaded the file; see app/core/exceptions.py.
            await documents.set_status(document, DocumentStatus.FAILED, error=error.message)
            log.warning("ingestion.failed", reason=error.code, error=error.message)
            return await _finish(session, task, TaskStatus.FAILED, {"error": error.message})
        except Exception:
            # Transient, or a bug. Either way arq should retry — but after the
            # last attempt nobody will, and a document left in `processing`
            # forever is a document whose owner never learns anything. So the
            # final try records the failure before re-raising.
            await _fail_if_last_attempt(ctx, session, documents, document, task)
            raise

        try:
            chunk_count = await _index(session, ctx, document, text)
        except (DocumentIngestionError, StorageError) as error:
            await documents.set_status(document, DocumentStatus.FAILED, error=error.message)
            log.warning("ingestion.indexing_failed", reason=error.code, error=error.message)
            return await _finish(session, task, TaskStatus.FAILED, {"error": error.message})
        except Exception:
            await _fail_if_last_attempt(ctx, session, documents, document, task)
            raise

        await documents.set_status(document, DocumentStatus.READY, error=None)
        log.info("ingestion.succeeded", characters=len(text), chunks=chunk_count)
        return await _finish(
            session,
            task,
            TaskStatus.SUCCEEDED,
            {"characters": len(text), "chunks": chunk_count},
        )


async def _extract(storage: ObjectStorage, document: Document) -> str:
    """Fetch the bytes and turn them into text, off the event loop.

    `asyncio.to_thread` is the whole reason `extract_text` is allowed to be a
    blocking function. A worker runs many jobs concurrently on one loop; a
    thirty-second PDF parse executed inline would stall every other job in the
    process, including the fast ones, for thirty seconds.
    """
    data = await storage.get(document.storage_uri)
    return await asyncio.to_thread(extract_text, data, document.mime_type)


async def _index(session: AsyncSession, ctx: dict[str, Any], document: Document, text: str) -> int:
    """Chunk the text, embed the chunks, and replace this document's index (M6).

    Three things here are worth more than they look.

    **Chunking runs off the event loop.** `chunk_text` is CPU-bound — tiktoken
    encodes the whole document, twice — and a worker runs many jobs on one
    loop. Inline, a large document would stall every other job in the process.

    **Chunks are replaced, not appended.** The task can be delivered twice, and
    appending would double every chunk. Duplicates do not announce themselves;
    they quietly occupy two of the top five slots in every search that matches
    them. This is the promise M5's docstring made and could not yet keep.

    **The vector width is checked before the insert.** A provider returning
    3072 dimensions into a `vector(1536)` column fails inside Postgres with a
    message about the column, several layers below the misconfiguration that
    caused it. Checking here names the real problem.
    """
    settings: Settings = ctx["settings"]
    embedder: EmbeddingProvider = ctx["embedder"]

    chunks = await asyncio.to_thread(
        chunk_text,
        text,
        chunk_size=settings.chunk_size_tokens,
        overlap=settings.chunk_overlap_tokens,
    )

    if not chunks:
        # Extraction already refuses empty documents, so reaching this means
        # text survived that check and still produced nothing to index.
        message = "The document produced no indexable text."
        raise DocumentIngestionError(message)

    vectors = await embedder.embed_documents([chunk.content for chunk in chunks])

    if len(vectors) != len(chunks):
        message = f"Embedding provider returned {len(vectors)} vectors for {len(chunks)} chunks."
        raise DocumentIngestionError(message)

    if vectors and len(vectors[0]) != EMBEDDING_DIMENSIONS:
        message = (
            f"Embedding provider returned {len(vectors[0])} dimensions, but "
            f"document_chunks.embedding is vector({EMBEDDING_DIMENSIONS}). "
            "Check EMBEDDING_MODEL and EMBEDDING_DIMENSIONS."
        )
        raise DocumentIngestionError(message)

    return await ChunkRepository(session).replace_for_document(
        document.id,
        [
            DocumentChunk(
                document_id=document.id,
                chunk_index=chunk.index,
                content=chunk.content,
                token_count=chunk.token_count,
                embedding=vector,
                chunk_metadata={"mime_type": document.mime_type},
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ],
    )


async def _start_task(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    """Mark the mirror row `running` and count the attempt.

    Committed immediately rather than at the end, so `attempts` survives a
    crash. A counter that is only written on success counts successes, which is
    not what anyone reads it for — the question is always "how many times has
    this already failed?".

    A missing task row is tolerated: the job can still do its real work, and
    refusing to ingest a document because its bookkeeping row was deleted would
    be the bookkeeping tail wagging the dog.
    """
    task = await session.get(Task, task_id)

    if task is None:
        logger.warning("ingestion.task_row_missing", task_id=str(task_id))
        return None

    task.status = TaskStatus.RUNNING
    task.attempts += 1
    await session.commit()
    return task


async def _finish(
    session: AsyncSession,
    task: Task | None,
    status: TaskStatus,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Write the terminal state of both rows and commit once.

    Returns the same dict arq stores as the job result, so the operator's two
    views — `tasks.result` in Postgres and `job.result()` in Redis — cannot
    disagree.
    """
    if task is not None:
        task.status = status
        task.result = result

    await session.commit()
    return result


async def _fail_if_last_attempt(
    ctx: dict[str, Any],
    session: AsyncSession,
    documents: DocumentRepository,
    document: Document,
    task: Task | None,
) -> None:
    """On the final retry, record the failure before letting arq give up.

    Without this the retry policy has a hole shaped exactly like its own
    success criteria: the first two failures are invisible *and correct*
    (something will try again), and the third is invisible and wrong (nothing
    will). The user sees `processing` in both cases and can never tell them
    apart.
    """
    job_try = int(ctx.get("job_try", 1))
    max_tries = int(ctx.get("max_tries", 1))

    if job_try < max_tries:
        return

    message = "Ingestion failed repeatedly. Try uploading the file again."
    await documents.set_status(document, DocumentStatus.FAILED, error=message)
    await _finish(session, task, TaskStatus.FAILED, {"error": message, "attempts": job_try})
