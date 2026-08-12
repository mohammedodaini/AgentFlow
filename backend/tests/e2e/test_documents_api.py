"""/api/v1/documents over HTTP (M5).

End-to-end: real routing, real dependencies, real auth, real database, real
local storage. Only the arq queue is substituted, by a recorder — a real one
would need a worker running and would turn every upload assertion into a wait.

The test this file exists for is `test_the_job_is_enqueued_after_the_commit`.
Everything else is ordinary endpoint coverage; that one pins a property the
whole 202 design rests on and that no code in this repository controls.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import Document, DocumentStatus, Organization
from app.models.task import INGEST_DOCUMENT
from tests.conftest import RecordingQueue
from tests.factories import DEFAULT_PASSWORD, TEXT_BYTES, make_document, make_pdf

PDF_UPLOAD = {"file": ("handbook.pdf", make_pdf(), "application/pdf")}


async def register(client: AsyncClient) -> dict[str, str]:
    """Register a user and return headers that can act inside their tenant.

    Registration creates the user, a personal organization and an owner
    membership in one transaction (M3), so this is the shortest honest route to
    an authenticated, org-scoped caller.
    """
    email = f"{uuid.uuid4().hex}@example.com"
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": DEFAULT_PASSWORD, "full_name": "Ada"},
    )
    assert response.status_code == HTTPStatus.CREATED, response.text
    token = response.json()["access_token"]

    organizations = await client.get(
        "/api/v1/organizations", headers={"Authorization": f"Bearer {token}"}
    )
    # `MembershipRead` nests the organization rather than flattening its id —
    # it answers "which workspaces, and as what?", so the role sits beside the
    # whole organization object.
    organization_id = organizations.json()[0]["organization"]["id"]

    return {"Authorization": f"Bearer {token}", "X-Organization-Id": organization_id}


# --------------------------------------------------------------------------
# upload
# --------------------------------------------------------------------------


async def test_upload_answers_202_with_a_pending_document(
    client: AsyncClient, queue: RecordingQueue
) -> None:
    """202, not 201.

    201 would claim the resource is finished and available. It is not — nothing
    has been parsed, and from M6 nothing will be searchable either. 202 says
    "accepted, not complete", which is exactly what has happened.
    """
    headers = await register(client)

    response = await client.post("/api/v1/documents", headers=headers, files=PDF_UPLOAD)

    assert response.status_code == HTTPStatus.ACCEPTED, response.text
    body = response.json()
    assert body["status"] == DocumentStatus.PENDING
    assert body["title"] == "handbook.pdf"
    assert body["mime_type"] == "application/pdf"
    assert body["error"] is None

    assert len(queue.jobs) == 1
    job = queue.jobs[0]
    assert job["function"] == INGEST_DOCUMENT
    assert job["document_id"] == body["id"]


async def test_the_response_never_leaks_the_storage_key(client: AsyncClient) -> None:
    """`storage_uri` is on the model and deliberately absent from the schema.

    Publishing it would freeze a private key layout into the public API and
    hand out exactly what someone would need if a bucket were ever
    misconfigured.
    """
    headers = await register(client)

    response = await client.post("/api/v1/documents", headers=headers, files=PDF_UPLOAD)

    assert "storage_uri" not in response.json()
    assert "organization_id" not in response.json()


async def test_the_job_is_enqueued_after_the_commit(
    client: AsyncClient, lifecycle_events: list[str]
) -> None:
    """The ordering the entire 202 flow depends on.

    Enqueue before commit and a worker can dequeue the job, look for the
    document, and find nothing — leaving it permanently `pending`, with the
    only evidence a worker log line about a row that plainly exists.

    This test earned its place immediately. The first implementation used
    `background_tasks.add_task`, on the documented belief that dependency
    teardown runs before background tasks. On FastAPI 0.141 / Starlette 1.6 it
    is the other way round, and this assertion failed with
    `['enqueue', 'commit']` — the exact race, reproduced on the first run. The
    route now commits explicitly before enqueueing.

    Ordering guarantees that belong to the framework rather than to us are
    exactly the ones worth asserting: a comment claiming this would have gone
    on being wrong indefinitely.
    """
    headers = await register(client)
    lifecycle_events.clear()

    await client.post("/api/v1/documents", headers=headers, files=PDF_UPLOAD)

    # A trailing "commit" from `get_db`'s teardown follows; it is a no-op here,
    # and only the first two entries carry the guarantee.
    assert lifecycle_events[:2] == ["commit", "enqueue"]


async def test_an_unsupported_type_is_415_with_the_house_error_body(
    client: AsyncClient, queue: RecordingQueue
) -> None:
    """415 rather than a generic 400: it tells the client to send something
    *else*, where 413 tells it to send *less*. Collapsing them would throw away
    the only actionable part of the answer."""
    headers = await register(client)

    response = await client.post(
        "/api/v1/documents",
        headers=headers,
        files={"file": ("virus.exe", b"MZ\x90\x00", "application/x-msdownload")},
    )

    assert response.status_code == HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    assert response.json()["error"]["code"] == "unsupported_media_type"
    assert "application/pdf" in response.json()["error"]["message"]
    assert queue.jobs == [], "nothing may be queued for a rejected upload"


async def test_a_file_over_the_limit_is_413(client: AsyncClient, app: FastAPI) -> None:
    """The limit is overridden through the settings dependency rather than the
    environment, because the app is already built by the time a test body runs
    — and this exercises the real route either way."""
    headers = await register(client)

    def tiny_settings() -> Settings:
        return get_settings().model_copy(update={"max_upload_bytes": 8})

    app.dependency_overrides[get_settings] = tiny_settings

    response = await client.post(
        "/api/v1/documents",
        headers=headers,
        files={"file": ("big.txt", TEXT_BYTES, "text/plain")},
    )

    assert response.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_upload_requires_authentication(client: AsyncClient) -> None:
    """The dependency fails closed: a route without `CurrentMembership` in its
    signature would be visibly public, and this one is not."""
    response = await client.post("/api/v1/documents", files=PDF_UPLOAD)

    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_upload_requires_an_organization_header(client: AsyncClient) -> None:
    """422 from FastAPI's own validation. A default organization would mean a
    mis-scoped request quietly succeeds against the wrong tenant — the worst
    available failure mode in a multi-tenant system (ADR-0005)."""
    headers = await register(client)
    del headers["X-Organization-Id"]

    response = await client.post("/api/v1/documents", headers=headers, files=PDF_UPLOAD)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


# --------------------------------------------------------------------------
# read, poll, delete
# --------------------------------------------------------------------------


async def test_polling_returns_whatever_the_worker_last_wrote(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The other half of the 202 pattern.

    The worker's write is simulated here rather than run —
    `tests/integration/test_ingestion_task.py` covers the worker itself. What
    this proves is that the API surfaces the result, including the failure
    message, which is the only way the user ever learns what happened.
    """
    headers = await register(client)
    created = await client.post("/api/v1/documents", headers=headers, files=PDF_UPLOAD)
    document_id = uuid.UUID(created.json()["id"])

    first_poll = await client.get(f"/api/v1/documents/{document_id}", headers=headers)
    assert first_poll.json()["status"] == DocumentStatus.PENDING

    document = await db_session.get(Document, document_id)
    assert document is not None
    document.status = DocumentStatus.FAILED
    document.error = "This PDF is password-protected."
    await db_session.flush()

    polled = await client.get(f"/api/v1/documents/{document_id}", headers=headers)
    assert polled.json()["status"] == DocumentStatus.FAILED
    assert polled.json()["error"] == "This PDF is password-protected."


async def test_listing_returns_the_page_envelope(client: AsyncClient) -> None:
    """A bare array cannot tell "2 documents" from "2 of 4,000", and leaves
    nowhere to add a total later without breaking every consumer."""
    headers = await register(client)
    await client.post("/api/v1/documents", headers=headers, files=PDF_UPLOAD)
    await client.post(
        "/api/v1/documents", headers=headers, files={"file": ("n.txt", TEXT_BYTES, "text/plain")}
    )

    response = await client.get("/api/v1/documents", headers=headers)

    body = response.json()
    assert body["total"] == 2
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert len(body["items"]) == 2


async def test_an_unknown_status_filter_is_rejected(client: AsyncClient) -> None:
    """Typed as the enum, so a typo is a 422 rather than a silently empty page
    that looks exactly like "you have no documents"."""
    headers = await register(client)

    response = await client.get("/api/v1/documents?status=nonsense", headers=headers)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_another_tenants_document_is_a_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Not 403. A different status for "exists but not yours" turns the
    endpoint into an oracle for enumerating ids across tenants."""
    headers = await register(client)

    other = Organization(name="Other Co", slug=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    await db_session.flush()
    theirs = await make_document(db_session, organization=other)

    response = await client.get(f"/api/v1/documents/{theirs.id}", headers=headers)

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["error"]["code"] == "not_found"


async def test_delete_returns_204_and_removes_the_document(client: AsyncClient) -> None:
    headers = await register(client)
    created = await client.post("/api/v1/documents", headers=headers, files=PDF_UPLOAD)
    document_id = created.json()["id"]

    response = await client.delete(f"/api/v1/documents/{document_id}", headers=headers)

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert not response.content

    gone = await client.get(f"/api/v1/documents/{document_id}", headers=headers)
    assert gone.status_code == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize("method", ["get", "delete"])
async def test_an_unknown_document_id_is_a_404(client: AsyncClient, method: str) -> None:
    headers = await register(client)

    response = await getattr(client, method)(f"/api/v1/documents/{uuid.uuid4()}", headers=headers)

    assert response.status_code == HTTPStatus.NOT_FOUND
