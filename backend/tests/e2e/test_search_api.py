"""/api/v1/search over HTTP (M6).

The full round trip with nothing faked but the queue: register, POST a real
file, run the real ingestion against it, then ask a question and get the
passage back with a citation. Every layer the milestone touched is in the
path — upload, storage, extraction, chunking, embedding, pgvector, and the
tenancy join.

What makes this worth having on top of `tests/integration/test_retrieval.py` is
that the chunks these queries rank are the chunks the *ingestion pipeline*
wrote, not rows a test placed by hand to match its own query. The integration
tests prove the SQL ranks correctly; this proves the thing a user uploads is
the thing they can later find.

The worker is invoked directly rather than through arq, exactly as the
integration tests do (see `tests/worker_harness.py`): booting arq would test
arq, and the ordering it would exercise is already pinned by
`test_the_job_is_enqueued_after_the_commit`.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentStatus
from app.models.task import Task
from app.storage import ObjectStorage
from tests.factories import DEFAULT_PASSWORD
from tests.worker_harness import run, worker_context

HANDBOOK = b"""Expenses are reimbursed monthly, provided a receipt is attached to the claim.

Holiday requests must be approved by a line manager at least two weeks ahead.

The office plants are watered every Tuesday by the facilities team.
"""

EXPENSES_SENTENCE = "Expenses are reimbursed monthly"


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
    organization_id = organizations.json()[0]["organization"]["id"]

    return {"Authorization": f"Bearer {token}", "X-Organization-Id": organization_id}


async def upload_and_ingest(
    client: AsyncClient,
    session: AsyncSession,
    storage: ObjectStorage,
    headers: dict[str, str],
    *,
    data: bytes = HANDBOOK,
    filename: str = "handbook.txt",
) -> Document:
    """POST a file, then run the ingestion the queue would have run.

    The queue is a recorder in tests, so nothing else would move the document
    out of `pending`. The task row is looked up rather than invented, so if the
    route ever stopped creating one this fails here rather than searching an
    empty index and reporting "no results" as though that were the answer.
    """
    response = await client.post(
        "/api/v1/documents", headers=headers, files={"file": (filename, data, "text/plain")}
    )
    assert response.status_code == HTTPStatus.ACCEPTED, response.text

    document = await session.get(Document, uuid.UUID(response.json()["id"]))
    assert document is not None

    task = await session.scalar(
        select(Task).where(Task.payload["document_id"].astext == str(document.id))
    )
    assert task is not None, "the upload must have recorded a task row"

    await run(worker_context(session, storage), document, task)
    assert document.status is DocumentStatus.READY, document.error

    return document


# --------------------------------------------------------------------------
# the round trip
# --------------------------------------------------------------------------


async def test_an_uploaded_document_becomes_searchable(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The milestone in one test: upload a file, then find a sentence in it.

    Asserting on *which* passage came back rather than merely that something
    did. A search that returns the wrong chunk with a confident score is the
    failure this whole layer exists to prevent, and it passes any test that
    only counts rows.
    """
    headers = await register(client)
    document = await upload_and_ingest(client, db_session, storage, headers)

    response = await client.post(
        "/api/v1/search", headers=headers, json={"query": "expense receipt reimbursed"}
    )

    assert response.status_code == HTTPStatus.OK, response.text
    results = response.json()
    assert results, "an indexed document must be findable"
    assert EXPENSES_SENTENCE in results[0]["content"]
    assert results[0]["document_id"] == str(document.id)
    assert results[0]["document_title"] == "handbook.txt"
    assert 0.0 <= results[0]["score"] <= 1.0


async def test_results_come_back_ranked(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """Descending relevance, asserted over the wire.

    Inverting the distance-to-similarity conversion is a one-character mistake
    that ranks the *worst* matches first, and every result still looks
    superficially plausible in the response body.
    """
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)

    response = await client.post(
        "/api/v1/search", headers=headers, json={"query": "holiday manager approval", "top_k": 3}
    )

    scores = [result["score"] for result in response.json()]
    assert scores == sorted(scores, reverse=True)


async def test_top_k_is_honoured(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)

    response = await client.post(
        "/api/v1/search", headers=headers, json={"query": "office", "top_k": 1}
    )

    assert len(response.json()) == 1


async def test_search_is_a_bare_list_not_a_page(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """A ranked top-k is not a paginated collection.

    There is no meaningful total — every chunk in the corpus has *some*
    similarity — and offering `offset` would invite clients to page through a
    ranking whose tail is noise by construction.
    """
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)

    response = await client.post("/api/v1/search", headers=headers, json={"query": "expenses"})

    assert isinstance(response.json(), list)


# --------------------------------------------------------------------------
# tenancy — the failure that matters most
# --------------------------------------------------------------------------


async def test_a_search_never_reaches_another_tenants_documents(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """Two real tenants, two real uploads, over HTTP.

    The integration suite asserts the SQL join scopes correctly. This asserts
    that the request path actually *reaches* that join with the right
    organization — a route that read the id from the token's own default
    workspace instead of the header would pass every repository test and leak
    here.
    """
    theirs = await register(client)
    await upload_and_ingest(client, db_session, storage, theirs)

    mine = await register(client)
    await upload_and_ingest(
        client,
        db_session,
        storage,
        mine,
        data=b"Our team ships on Fridays and nothing else.\n",
        filename="ours.txt",
    )

    response = await client.post(
        "/api/v1/search", headers=mine, json={"query": "expense receipt reimbursed", "top_k": 10}
    )

    contents = [result["content"] for result in response.json()]
    assert contents, "our own document must still be searchable"
    assert all(EXPENSES_SENTENCE not in content for content in contents)


async def test_a_tenant_with_nothing_indexed_gets_an_empty_list(client: AsyncClient) -> None:
    """Not an error, and not somebody else's chunks."""
    headers = await register(client)

    response = await client.post("/api/v1/search", headers=headers, json={"query": "expenses"})

    assert response.status_code == HTTPStatus.OK
    assert response.json() == []


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


async def test_search_requires_authentication(client: AsyncClient) -> None:
    response = await client.post("/api/v1/search", json={"query": "expenses"})

    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_search_requires_an_organization_header(client: AsyncClient) -> None:
    """A default organization would mean a mis-scoped search quietly succeeds
    against the wrong tenant — the worst available failure mode here
    (ADR-0005)."""
    headers = await register(client)
    del headers["X-Organization-Id"]

    response = await client.post("/api/v1/search", headers=headers, json={"query": "expenses"})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"query": ""}, id="empty-query"),
        pytest.param({"query": "x" * 2001}, id="query-too-long"),
        pytest.param({"query": "expenses", "top_k": 0}, id="top-k-zero"),
        pytest.param({"query": "expenses", "top_k": 1000}, id="top-k-over-the-ceiling"),
        pytest.param({}, id="no-query-at-all"),
    ],
)
async def test_an_invalid_body_is_422(client: AsyncClient, body: dict[str, object]) -> None:
    """Rejected at the schema, before anything is embedded.

    Each of these costs something if it gets through: an empty query embeds to
    a vector that matches arbitrarily, an unbounded one is billable text
    somebody else chose the size of, and an unbounded `top_k` turns one request
    into a table scan whose every row is context paid for at M7.
    """
    headers = await register(client)

    response = await client.post("/api/v1/search", headers=headers, json=body)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
