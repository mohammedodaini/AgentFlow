"""/api/v1/agent-runs over HTTP (M9).

Upload a document, ask the agent about it, and read the trace back — through
real routing, real auth, real pgvector.

The assertions that matter most here are about *what the API refuses to show*.
`agent_runs.checkpoint` holds LangGraph's serialised internal state, including
the full text of every retrieved chunk; publishing it would freeze the graph's
internals into a contract that could never change, and inflate every response by
a corpus.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.rag.graph import GENERATE, PREPARE, RETRIEVE
from app.agents.rag.tools import SEARCH_CHUNKS
from app.models.agent_run import RunStatus
from app.storage import ObjectStorage
from tests.e2e.test_search_api import register, upload_and_ingest

QUESTION = "How are expenses reimbursed?"


async def ask_agent(
    client: AsyncClient, headers: dict[str, str], question: str = QUESTION
) -> dict[str, Any]:
    response = await client.post("/api/v1/agent-runs", headers=headers, json={"question": question})
    assert response.status_code == HTTPStatus.OK, response.text
    body: dict[str, Any] = response.json()
    return body


# --------------------------------------------------------------------------
# running the agent
# --------------------------------------------------------------------------


async def test_the_agent_answers_and_returns_its_trace(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The milestone over HTTP: upload, ask, and get back both the answer and
    the record of how it was reached."""
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)

    body = await ask_agent(client, headers)

    assert body["status"] == RunStatus.SUCCEEDED
    assert body["agent_name"] == "rag"
    assert "reimbursed" in body["output"]["answer"]
    assert body["output"]["citations"]
    assert [step["node_name"] for step in body["steps"]] == [PREPARE, RETRIEVE, GENERATE]


async def test_the_trace_is_returned_to_the_client_not_only_the_operator(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """Specific to AI products: when an answer looks wrong, "what did it search
    for, and what came back?" is often a question the *user* can answer faster
    than we can. An interface that shows its working earns trust that a bare
    answer does not."""
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)

    body = await ask_agent(client, headers)
    retrieval = next(step for step in body["steps"] if step["node_name"] == RETRIEVE)

    assert retrieval["tool_name"] == SEARCH_CHUNKS
    assert retrieval["tool_input"]["query"] == QUESTION
    assert retrieval["tool_output"]["count"] > 0
    assert retrieval["latency_ms"] >= 0


async def test_the_response_never_leaks_the_graph_checkpoint(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """`checkpoint` is on the model and deliberately absent from every schema.

    It is LangGraph's internal state — publishing it would freeze the graph's
    internals into a public contract, and carry the whole retrieved corpus in
    every response.
    """
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)

    body = await ask_agent(client, headers)

    assert "checkpoint" not in body
    assert "organization_id" not in body


async def test_usage_is_reported_per_run(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The unit of observability and the unit of billing are the same row, so a
    run nobody can price is a run nobody can explain."""
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)

    body = await ask_agent(client, headers)

    assert body["total_tokens"] > 0
    assert body["duration_ms"] is not None


# --------------------------------------------------------------------------
# reading runs back
# --------------------------------------------------------------------------


async def test_a_run_can_be_fetched_again_with_its_trace(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The endpoint M12 will poll while a run waits for approval. It exists now,
    before anything needs to poll it, so the shape will not have to change."""
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)
    created = await ask_agent(client, headers)

    response = await client.get(f"/api/v1/agent-runs/{created['id']}", headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert response.json()["id"] == created["id"]
    assert response.json()["steps"] == created["steps"]


async def test_listing_returns_summaries_without_traces(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """A listing of twenty runs each dragging its full trace would transfer
    megabytes to render a table showing none of it."""
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)
    await ask_agent(client, headers)
    await ask_agent(client, headers, "holiday approval")

    body = (await client.get("/api/v1/agent-runs", headers=headers)).json()

    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert "steps" not in body["items"][0]
    assert "output" not in body["items"][0]


async def test_listing_can_be_filtered_by_status(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)
    await ask_agent(client, headers)

    succeeded = await client.get("/api/v1/agent-runs?status=succeeded", headers=headers)
    failed = await client.get("/api/v1/agent-runs?status=failed", headers=headers)

    assert succeeded.json()["total"] == 1
    assert failed.json()["total"] == 0


async def test_an_unknown_status_filter_is_rejected(client: AsyncClient) -> None:
    """Typed as the enum, so a typo is a 422 rather than a silently empty page
    that looks exactly like "you have no runs"."""
    headers = await register(client)

    response = await client.get("/api/v1/agent-runs?status=nonsense", headers=headers)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


# --------------------------------------------------------------------------
# tenancy and refusals
# --------------------------------------------------------------------------


async def test_the_agent_never_reaches_another_tenants_documents(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The organization is closed over when the tool is built and never appears
    in the schema the model sees, so no tool call can name a different tenant —
    whatever a prompt-injected document asks for."""
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

    body = await ask_agent(client, mine)

    assert "reimbursed" not in body["output"]["answer"]
    assert all(citation["document_title"] == "ours.txt" for citation in body["output"]["citations"])


async def test_another_tenants_run_is_a_404(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """Not 403. A different status for "exists but not yours" turns the endpoint
    into an oracle for enumerating run ids across tenants."""
    theirs = await register(client)
    await upload_and_ingest(client, db_session, storage, theirs)
    created = await ask_agent(client, theirs)

    mine = await register(client)

    response = await client.get(f"/api/v1/agent-runs/{created['id']}", headers=mine)

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["error"]["code"] == "not_found"


async def test_an_unknown_run_id_is_a_404(client: AsyncClient) -> None:
    headers = await register(client)

    response = await client.get(f"/api/v1/agent-runs/{uuid.uuid4()}", headers=headers)

    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_running_the_agent_requires_authentication(client: AsyncClient) -> None:
    response = await client.post("/api/v1/agent-runs", json={"question": "x"})

    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_reading_runs_requires_authentication(client: AsyncClient) -> None:
    """Separate from the POST case rather than parametrised: `client.get` takes
    no `json` argument, so one parametrised test over both verbs cannot express
    both requests."""
    assert (await client.get("/api/v1/agent-runs")).status_code == HTTPStatus.UNAUTHORIZED
    assert (
        await client.get(f"/api/v1/agent-runs/{uuid.uuid4()}")
    ).status_code == HTTPStatus.UNAUTHORIZED


async def test_running_the_agent_requires_an_organization_header(client: AsyncClient) -> None:
    """A default organization would run the agent against the wrong tenant's
    documents (ADR-0005)."""
    headers = await register(client)
    del headers["X-Organization-Id"]

    response = await client.post("/api/v1/agent-runs", headers=headers, json={"question": QUESTION})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"question": ""}, id="empty-question"),
        pytest.param({"question": "x" * 2001}, id="question-too-long"),
        pytest.param({"question": "expenses", "top_k": 0}, id="top-k-zero"),
        pytest.param({}, id="no-question"),
    ],
)
async def test_an_invalid_body_is_422(client: AsyncClient, body: dict[str, object]) -> None:
    """Rejected at the schema, before a run row is created. Otherwise every
    malformed request would leave a row nobody can explain."""
    headers = await register(client)

    response = await client.post("/api/v1/agent-runs", headers=headers, json=body)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_asking_with_nothing_uploaded_refuses_rather_than_inventing(
    client: AsyncClient,
) -> None:
    """A brand-new organization. The graph retrieves nothing, rewrites,
    retrieves nothing again, and refuses — with a trace showing all of it."""
    headers = await register(client)

    body = await ask_agent(client, headers)

    assert body["status"] == RunStatus.SUCCEEDED
    assert body["output"]["citations"] == []
    assert "could not find" in body["output"]["answer"]
    assert len(body["steps"]) > 2, "the retry path should be visible in the trace"


# --------------------------------------------------------------------------
# M15: one entry point
# --------------------------------------------------------------------------


async def supervise(
    client: AsyncClient, headers: dict[str, str], instruction: str
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/agent-runs/supervised", headers=headers, json={"instruction": instruction}
    )
    assert response.status_code == HTTPStatus.OK, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_one_endpoint_answers_a_question(client: AsyncClient) -> None:
    """The milestone over HTTP: the caller says nothing about which agent to use.

    Until M15 they had to — `/agent-runs` could not schedule and
    `/agent-runs/calendar` could not answer — which made the human the router.
    """
    headers = await register(client)

    body = await supervise(client, headers, "How are expenses reimbursed?")

    assert body["run"]["agent_name"] == "supervisor"
    assert body["delegated"]["agent_name"] == "rag"


async def test_one_endpoint_schedules(client: AsyncClient) -> None:
    headers = await register(client)

    body = await supervise(client, headers, "Schedule a design review on 2026-09-10 09:00")

    assert body["delegated"]["agent_name"] == "calendar"
    assert body["delegated"]["status"] == "paused_for_approval"
    # The approval row, not just the action. Returning the action alone was the
    # first shape of this response and it hid the M15 runtime bug: a paused run
    # with no row behind it looked identical to a working one.
    assert body["approval"]["status"] == "pending"
    assert body["approval"]["requested_action"]["kind"] == "calendar.create_event"
    assert body["approval"]["agent_run_id"] == body["delegated"]["id"]


async def test_one_endpoint_drafts_email(client: AsyncClient) -> None:
    headers = await register(client)

    body = await supervise(client, headers, "Email ada@example.com about Q3 saying it is ready")

    assert body["delegated"]["agent_name"] == "email"
    assert body["delegated"]["status"] == "paused_for_approval"
    assert body["approval"]["requested_action"]["to"] == "ada@example.com"


async def test_a_refusal_is_a_200_with_a_reason(client: AsyncClient) -> None:
    """Not a 4xx: the request was well-formed and understood, and the answer is
    that nothing here serves it. A 400 would tell a client to fix its request."""
    headers = await register(client)

    body = await supervise(client, headers, "Order me a taxi to the airport")

    assert body["delegated"] is None
    assert body["approval"] is None
    assert "documents" in body["reason"]


async def test_the_routing_decision_is_in_the_trace(client: AsyncClient) -> None:
    """Published deliberately. "Why did it do that?" is the first question about
    any router, and an unexplained decision is one nobody can argue with."""
    headers = await register(client)

    body = await supervise(client, headers, "Schedule a design review on 2026-09-10 09:00")
    nodes = [step["node_name"] for step in body["run"]["steps"]]

    assert nodes == ["classify", "plan"]
    assert body["run"]["output"]["plan"] == ["calendar"]


async def test_delegated_runs_are_listed_separately(client: AsyncClient) -> None:
    """Both runs exist in `/agent-runs`, under their own agent names — a
    supervised question is not a hidden sub-step of something else."""
    headers = await register(client)
    await supervise(client, headers, "How are expenses reimbursed?")

    listing = await client.get("/api/v1/agent-runs?limit=50", headers=headers)
    names = {item["agent_name"] for item in listing.json()["items"]}

    assert {"supervisor", "rag"} <= names


async def test_supervising_requires_authentication(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/agent-runs/supervised", json={"instruction": "How are expenses reimbursed?"}
    )

    assert response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}


async def test_an_empty_instruction_is_rejected_by_validation(client: AsyncClient) -> None:
    headers = await register(client)

    response = await client.post(
        "/api/v1/agent-runs/supervised", headers=headers, json={"instruction": ""}
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
