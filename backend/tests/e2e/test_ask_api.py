"""/api/v1/ask and /ask/stream over HTTP (M7).

The full round trip: register, upload a real file, run the real ingestion, then
ask a question and get a grounded answer with citations — through real routing,
real auth, real pgvector, and the real SSE framing.

The streaming tests earn their place more than the rest. Everything about
Server-Sent Events that can go wrong goes wrong *at the transport*: a missing
blank line between frames leaves the client waiting forever, a buffering layer
delivers nothing until the response ends, and a failure after the first byte can
never become a status code. None of that is visible from inside `Generator`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from http import HTTPStatus

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import get_llm
from app.llm.base import Completion, LLMError
from app.llm.offline import NO_CONTEXT_ANSWER
from app.storage import ObjectStorage
from tests.e2e.test_search_api import HANDBOOK, register, upload_and_ingest

EXPENSES_QUESTION = "How are expenses reimbursed?"


def events(body: str) -> list[tuple[str, object]]:
    """Parse an SSE body into `(event, data)` pairs.

    Written out rather than pulled from a library, because the framing *is* what
    is under test: if this parser and the endpoint disagree about where a frame
    ends, that disagreement is the bug.
    """
    parsed: list[tuple[str, object]] = []

    for frame in body.split("\n\n"):
        if not frame.strip():
            continue

        fields = dict(line.split(": ", 1) for line in frame.strip().splitlines())
        parsed.append((fields["event"], json.loads(fields["data"])))

    return parsed


def tokens_of(body: str) -> str:
    """Every `token` event, reassembled."""
    return "".join(
        data["text"]  # type: ignore[index]
        for name, data in events(body)
        if name == "token"
    )


# --------------------------------------------------------------------------
# POST /ask
# --------------------------------------------------------------------------


async def test_a_question_is_answered_from_the_uploaded_document(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The milestone, over HTTP: upload a handbook, ask about it, and get the
    passage back as an answer that cites its source."""
    headers = await register(client)
    document = await upload_and_ingest(client, db_session, storage, headers)

    response = await client.post("/api/v1/ask", headers=headers, json={"query": EXPENSES_QUESTION})

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert "reimbursed" in body["answer"]
    assert body["citations"]
    assert body["citations"][0]["document_id"] == str(document.id)
    assert body["citations"][0]["document_title"] == "handbook.txt"


async def test_the_answer_cites_a_source_the_client_can_resolve(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The bracketed marker in the prose and the citation list must agree.

    Two well-formed halves that disagree is the failure this guards, and it
    raises nothing: the answer says `[2]`, the API returns a `[2]` pointing at a
    different chunk, and every schema validates.
    """
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)

    body = (
        await client.post("/api/v1/ask", headers=headers, json={"query": EXPENSES_QUESTION})
    ).json()

    cited = body["answer"].rsplit("[", 1)[-1].rstrip("]")
    numbers = {str(citation["number"]) for citation in body["citations"]}

    assert cited in numbers, (body["answer"], numbers)


async def test_usage_is_returned_not_only_logged(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """A customer who can see what a question cost can make their own decisions
    about `top_k`. It also means M12 bills on the number the user was shown."""
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)

    body = (
        await client.post("/api/v1/ask", headers=headers, json={"query": EXPENSES_QUESTION})
    ).json()

    assert body["usage"]["input_tokens"] > 0
    assert body["usage"]["context_tokens"] > 0
    assert body["model"] == "offline-extractive"
    assert body["truncated"] is False


async def test_nothing_indexed_is_an_explicit_refusal_not_a_404(client: AsyncClient) -> None:
    """200 with a refusal, deliberately.

    Nothing is missing: the question was answered, and the answer is that the
    documents do not cover it. A 404 would say the *endpoint* was wrong, and a
    fluent invented answer would be very much worse than either.
    """
    headers = await register(client)

    response = await client.post("/api/v1/ask", headers=headers, json={"query": "anything"})

    assert response.status_code == HTTPStatus.OK
    assert response.json()["answer"] == NO_CONTEXT_ANSWER
    assert response.json()["citations"] == []


async def test_a_question_never_reaches_another_tenants_documents(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """Worse than the same leak at `/search`: there it returns another
    customer's passages, here it rewrites them in fluent prose."""
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

    body = (
        await client.post("/api/v1/ask", headers=mine, json={"query": EXPENSES_QUESTION})
    ).json()

    assert "reimbursed" not in body["answer"]
    assert all(citation["document_title"] == "ours.txt" for citation in body["citations"])


# --------------------------------------------------------------------------
# POST /ask/stream
# --------------------------------------------------------------------------


async def test_streaming_sends_sources_first_then_tokens_then_done(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The event contract a client codes against.

    Sources first, because retrieval completes before the first token exists and
    citations rendered late are citations nobody reads. `done` last, because
    without it a client cannot tell a finished answer from a dropped connection.
    """
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)

    response = await client.post(
        "/api/v1/ask/stream", headers=headers, json={"query": EXPENSES_QUESTION}
    )

    assert response.status_code == HTTPStatus.OK
    assert response.headers["content-type"].startswith("text/event-stream")

    names = [name for name, _ in events(response.text)]

    assert names[0] == "sources"
    assert names[-1] == "done"
    assert names.count("token") > 1, "a single token event would not exercise streaming"


async def test_the_streamed_tokens_reassemble_into_the_same_answer(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """Streaming is a delivery mechanism, not a different answer. If the two
    endpoints ever diverge, one is wrong and nobody would know which."""
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)

    whole = (
        await client.post("/api/v1/ask", headers=headers, json={"query": EXPENSES_QUESTION})
    ).json()["answer"]

    streamed = await client.post(
        "/api/v1/ask/stream", headers=headers, json={"query": EXPENSES_QUESTION}
    )

    assert tokens_of(streamed.text) == whole


async def test_streamed_sources_match_the_non_streamed_citations(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """Same evidence, same numbering, whichever endpoint asked."""
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)

    citations = (
        await client.post("/api/v1/ask", headers=headers, json={"query": EXPENSES_QUESTION})
    ).json()["citations"]

    streamed = await client.post(
        "/api/v1/ask/stream", headers=headers, json={"query": EXPENSES_QUESTION}
    )
    sources = next(data for name, data in events(streamed.text) if name == "sources")

    assert sources == citations


async def test_streaming_with_nothing_indexed_still_frames_correctly(
    client: AsyncClient,
) -> None:
    """The refusal path is a normal stream with no sources, so a client needs no
    special case and the route no branch."""
    headers = await register(client)

    response = await client.post("/api/v1/ask/stream", headers=headers, json={"query": "anything"})

    frames = events(response.text)

    assert frames[0] == ("sources", [])
    assert tokens_of(response.text) == NO_CONTEXT_ANSWER
    assert frames[-1][0] == "done"


async def test_a_failure_after_the_first_byte_becomes_an_error_event(
    client: AsyncClient, app: FastAPI, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The one thing streaming cannot do, handled explicitly.

    Once the first byte is written the status code has already been sent, so a
    model failure mid-answer *cannot* become a 502 — the response is a 200 that
    stops early. Without an explicit `error` event, a connection that died
    halfway is indistinguishable from one that finished, and the client renders
    a truncated answer as the complete one.

    The failure is injected at the provider, so the route's real error path
    runs rather than a simulated one.
    """
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)

    failing = FailsMidStream()
    app.dependency_overrides[get_llm] = lambda: failing

    response = await client.post(
        "/api/v1/ask/stream", headers=headers, json={"query": EXPENSES_QUESTION}
    )

    names = [name for name, _ in events(response.text)]

    assert response.status_code == HTTPStatus.OK, "the status was sent before the failure"
    assert tokens_of(response.text) == "Expenses are "
    assert names[-1] == "error", "a dead stream must not look like a finished one"
    assert "done" not in names

    error = next(data for name, data in events(response.text) if name == "error")
    assert error["code"] == "llm_unavailable"  # type: ignore[index]


class FailsMidStream:
    """Emits two chunks, then fails — the shape of a real timeout or rate limit
    partway through a long answer."""

    model = "fails-mid-stream"

    async def complete(self, *, system: str, prompt: str) -> Completion:  # pragma: no cover
        del system, prompt
        message = "unused"
        raise LLMError(message)

    async def stream(self, *, system: str, prompt: str) -> AsyncIterator[str]:
        del system, prompt
        yield "Expenses "
        yield "are "
        message = "The language model is unavailable. Please try again."
        raise LLMError(message)


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/v1/ask", "/api/v1/ask/stream"])
async def test_asking_requires_authentication(client: AsyncClient, path: str) -> None:
    response = await client.post(path, json={"query": "expenses"})

    assert response.status_code == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize("path", ["/api/v1/ask", "/api/v1/ask/stream"])
async def test_asking_requires_an_organization_header(client: AsyncClient, path: str) -> None:
    """A default organization would mean a mis-scoped question is answered from
    the wrong tenant's documents (ADR-0005)."""
    headers = await register(client)
    del headers["X-Organization-Id"]

    response = await client.post(path, headers=headers, json={"query": "expenses"})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"query": ""}, id="empty-question"),
        pytest.param({"query": "x" * 2001}, id="question-too-long"),
        pytest.param({"query": "expenses", "top_k": 0}, id="top-k-zero"),
        pytest.param({"query": "expenses", "top_k": 1000}, id="top-k-over-the-ceiling"),
        pytest.param({}, id="no-question-at-all"),
    ],
)
async def test_an_invalid_body_is_422(client: AsyncClient, body: dict[str, object]) -> None:
    """Rejected at the schema, before anything is embedded or generated. Each of
    these costs money if it gets through, and an unbounded `top_k` costs it
    twice — once retrieving, once as context."""
    headers = await register(client)

    response = await client.post("/api/v1/ask", headers=headers, json=body)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_the_handbook_fixture_still_says_what_these_tests_assume() -> None:
    """Guards the import: `HANDBOOK` comes from the M6 search tests, and a
    reword there would silently weaken every assertion above."""
    assert b"reimbursed" in HANDBOOK
