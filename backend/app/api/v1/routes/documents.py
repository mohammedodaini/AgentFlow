"""/documents — upload + status. THE 202 pattern lives here.

Layer: api. Upload stores bytes and a `pending` document row, then returns 202
immediately; a worker does the parsing. Parsing a 40-page PDF takes seconds,
and an HTTP request held open for seconds is a worker slot not serving anyone,
a connection a proxy will eventually kill, and a client with no way to find out
what happened after it does.

The one piece of real logic in this module
------------------------------------------
Two endpoints commit their session explicitly, which every other route in this
codebase is careful *not* to do — the commit boundary lives in `get_db`
(ADR-0006 / M2). They do it because they have an ordering requirement that a
commit-at-the-edge cannot express: work may only be advertised to the outside
world after it is durable.

Enqueue an ingestion job before the commit and a worker — which is fast, and
running in another process — can dequeue it, look for the document, and find
nothing. The document then stays `pending` forever, and the only evidence is a
worker log line about a row that plainly exists.

`BackgroundTasks` was the first attempt, on the belief that dependency teardown
runs before background tasks. On FastAPI 0.141 / Starlette 1.6 it does not: the
dependency exit stack closes *after* the response and its background tasks have
run, so the enqueue happened first. A test caught it —
`tests/e2e/test_documents_api.py::test_the_job_is_enqueued_after_the_commit`
asserts the real order rather than trusting a comment, and would have caught it
in production otherwise. See ADR-0008.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentMembership
from app.core.config import Settings, get_settings
from app.db.deps import get_db
from app.models.document import DocumentStatus
from app.schemas.common import Page
from app.schemas.document import DocumentRead
from app.services.document_service import DocumentService
from app.storage import ObjectStorage, get_storage
from app.workers.queue import JobQueue, enqueue_ingestion, get_queue

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

SessionDep = Annotated[AsyncSession, Depends(get_db)]
StorageDep = Annotated[ObjectStorage, Depends(get_storage)]
QueueDep = Annotated[JobQueue, Depends(get_queue)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _service(session: AsyncSession, storage: ObjectStorage, settings: Settings) -> DocumentService:
    """One place that knows how to build the service.

    Four endpoints construct it identically; a constructor call repeated four
    times is four places to edit when it grows a collaborator.
    """
    return DocumentService(session, storage, settings)


@router.post("", status_code=HTTPStatus.ACCEPTED, summary="Upload a document")
async def upload_document(
    membership: CurrentMembership,
    session: SessionDep,
    storage: StorageDep,
    queue: QueueDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File(description="PDF, plain text or markdown")],
) -> DocumentRead:
    """Accept a file and return 202 with a document in `pending`.

    202, not 201. 201 would claim the resource is finished and available, and
    it is not — nothing has been parsed yet, and at M6 nothing will be
    searchable yet either. 202 says "accepted, not complete", which is exactly
    true, and it tells the client to poll `GET /documents/{id}`.

    `CurrentMembership`, not `CurrentUser`: a document belongs to an
    organization, so the request must name which one (`X-Organization-Id`) and
    prove the caller belongs to it. Any member may upload — this is not a
    privileged action, and requiring admin would mean the people who actually
    have the documents cannot add them.
    """
    document, task = await _service(session, storage, settings).upload(
        organization_id=membership.organization_id,
        uploaded_by=membership.user_id,
        upload=file,
    )

    # Durable first, advertised second. See the module docstring.
    await session.commit()

    try:
        await enqueue_ingestion(
            queue,
            document_id=document.id,
            organization_id=membership.organization_id,
            task_id=task.id,
        )
    except (OSError, RedisError):
        # The queue is unreachable, and the document is already committed, so
        # there is nothing left to roll back. Answering 500 would be a lie in
        # the more damaging direction: the upload *was* accepted and the bytes
        # *are* stored, so a client that retried would create a duplicate.
        #
        # 202 stays honest, because the `tasks` row is committed too and says
        # `queued`. That row is precisely what a sweeper re-enqueues, and it is
        # the reason the table exists at all rather than trusting Redis to
        # remember (app/models/task.py).
        logger.exception(
            "documents.enqueue_failed", document_id=str(document.id), task_id=str(task.id)
        )

    return DocumentRead.model_validate(document)


@router.get("", summary="List documents")
async def list_documents(
    membership: CurrentMembership,
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
    status: Annotated[DocumentStatus | None, Query(description="Filter by status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[DocumentRead]:
    """The tenant's documents, newest first.

    `status` is a query parameter rather than four endpoints (`/pending`,
    `/failed`, …) because it is a filter on one collection, not four
    collections. Typed as the enum, so an unknown value is a 422 from FastAPI's
    own validation instead of a silently empty page — which is what a bare
    `str` would produce, and which looks identical to "you have no documents".
    """
    documents, total = await _service(session, storage, settings).list(
        membership.organization_id, status=status, limit=limit, offset=offset
    )

    return Page[DocumentRead](
        items=[DocumentRead.model_validate(document) for document in documents],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{document_id}", summary="Poll one document's ingestion status")
async def get_document(
    document_id: uuid.UUID,
    membership: CurrentMembership,
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> DocumentRead:
    """The other half of the 202 pattern: how a client finds out what happened.

    Polling, not a webhook or a socket, because polling is the only mechanism
    that needs no infrastructure at all and degrades to "the user refreshes the
    page". A WebSocket push is the M13 improvement, and it is an improvement,
    not a replacement — this endpoint still has to exist for the client that
    reconnects and asks "what did I miss?".
    """
    document = await _service(session, storage, settings).get(
        membership.organization_id, document_id
    )
    return DocumentRead.model_validate(document)


@router.delete("/{document_id}", status_code=HTTPStatus.NO_CONTENT, summary="Delete a document")
async def delete_document(
    document_id: uuid.UUID,
    membership: CurrentMembership,
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> Response:
    """Remove the row, commit, and only then remove the bytes.

    The same explicit commit as upload, for the mirror-image reason. Delete the
    object first and then fail to commit, and a surviving row points at bytes
    that are gone — a broken invariant, and one nothing will ever detect until
    a user opens the document. Delete the row first and fail to remove the
    bytes, and the cost is an orphaned file: money, not correctness.

    From M6 the chunks go with the row, via `ON DELETE CASCADE` — one statement
    in the database rather than a loop in Python that can be interrupted
    halfway.
    """
    storage_uri = await _service(session, storage, settings).delete(
        membership.organization_id, document_id
    )

    await session.commit()
    await storage.delete(storage_uri)

    return Response(status_code=HTTPStatus.NO_CONTENT)
