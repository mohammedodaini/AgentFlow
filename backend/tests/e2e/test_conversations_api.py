"""/api/v1/conversations over HTTP (M10).

Start a thread, ask a question, ask a follow-up that only makes sense given the
first — through real routing, real auth, real pgvector.

The two assertions worth the whole file:

- **the follow-up is answered**, which requires the question to have been made
  searchable on its own before it reached the index; and
- **the extraction job is enqueued after the commit**, because the worker reads
  the conversation back out of Postgres and a job delivered early would find the
  thread without the exchange it exists to learn from — and would succeed, having
  learned nothing, with no error anywhere.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.rag.tools import SEARCH_CHUNKS
from app.models.agent_run import AgentRun
from app.models.message import MessageRole
from app.models.task import EXTRACT_MEMORIES, Task, TaskStatus
from app.storage import ObjectStorage
from tests.conftest import RecordingQueue
from tests.e2e.test_search_api import register, upload_and_ingest

MILEAGE_HANDBOOK = b"""Mileage is reimbursed at 45p per mile for the first 10,000 miles.

Holiday requests must be approved by a line manager at least two weeks ahead.

The office plants are watered every Tuesday by the facilities team.
"""


async def start(client: AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post("/api/v1/conversations", headers=headers, json={})
    assert response.status_code == HTTPStatus.OK, response.text
    conversation_id: str = response.json()["id"]
    return conversation_id


async def say(
    client: AsyncClient, headers: dict[str, str], conversation_id: str, content: str
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=headers,
        json={"content": content},
    )
    assert response.status_code == HTTPStatus.OK, response.text
    body: dict[str, Any] = response.json()
    return body


async def searched_query(client: AsyncClient, headers: dict[str, str], turn: dict[str, Any]) -> str:
    """What the retrieve step actually sent to the index, from the run's trace.

    Read through the API rather than the database on purpose: the trace being a
    client-facing surface is ADR-0012's claim, and a test that only trusted it
    through SQL would not be exercising the claim.
    """
    response = await client.get(
        f"/api/v1/agent-runs/{turn['assistant_message']['agent_run_id']}", headers=headers
    )
    assert response.status_code == HTTPStatus.OK, response.text
    retrieve = next(step for step in response.json()["steps"] if step["tool_name"] == SEARCH_CHUNKS)
    query: str = retrieve["tool_input"]["query"]
    return query


# --------------------------------------------------------------------------
# a turn
# --------------------------------------------------------------------------


async def test_a_turn_returns_both_messages_and_persists_them(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The client has just sent text and needs the persisted id of its own turn
    to render it without guessing — returning only the reply would force an
    immediate re-fetch of the transcript."""
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)
    conversation_id = await start(client, headers)

    body = await say(client, headers, conversation_id, "How are expenses reimbursed?")

    assert body["user_message"]["role"] == MessageRole.USER
    assert body["assistant_message"]["role"] == MessageRole.ASSISTANT
    assert "reimbursed" in body["assistant_message"]["content"]

    transcript = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=headers
    )
    assert [message["role"] for message in transcript.json()] == ["user", "assistant"]


async def test_the_reply_links_to_the_run_that_produced_it(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """`messages.agent_run_id` is the bridge from a sentence in a chat window to
    the trace behind it. Without it, ADR-0012's "the trace is a client-facing
    surface" argument breaks down at exactly the place users would use it."""
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)
    conversation_id = await start(client, headers)

    body = await say(client, headers, conversation_id, "How are expenses reimbursed?")
    run_id = body["assistant_message"]["agent_run_id"]

    assert run_id is not None
    trace = await client.get(f"/api/v1/agent-runs/{run_id}", headers=headers)
    assert trace.status_code == HTTPStatus.OK
    assert trace.json()["steps"]


async def test_the_run_records_which_thread_it_belonged_to(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """`agent_runs.conversation_id` — the column M9 deliberately deferred because
    `conversations` did not exist yet."""
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)
    conversation_id = await start(client, headers)

    body = await say(client, headers, conversation_id, "How are expenses reimbursed?")

    run = await db_session.get(AgentRun, uuid.UUID(body["assistant_message"]["agent_run_id"]))
    assert run is not None
    assert run.conversation_id == uuid.UUID(conversation_id)


async def test_the_title_is_derived_from_the_first_message(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """By code, not by a model. An LLM-titled thread would add a model call, its
    latency and its failure mode to the act of sending a first message."""
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)
    conversation_id = await start(client, headers)

    body = await say(client, headers, conversation_id, "How are expenses reimbursed?")

    assert body["conversation"]["title"] == "How are expenses reimbursed?"


async def test_a_later_message_does_not_rename_the_thread(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """A title that changed with every turn would make a sidebar unusable."""
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)
    conversation_id = await start(client, headers)

    await say(client, headers, conversation_id, "How are expenses reimbursed?")
    body = await say(client, headers, conversation_id, "And holiday?")

    assert body["conversation"]["title"] == "How are expenses reimbursed?"


# --------------------------------------------------------------------------
# history — the milestone
# --------------------------------------------------------------------------


async def test_a_follow_up_that_only_makes_sense_in_context_is_answered(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The milestone, in one test.

    "How much is that?" is three words with no subject. Retrieval sees one query,
    not a thread, so without contextualisation it matches nothing in particular
    and the agent refuses. Asked inside a conversation it has to work.
    """
    headers = await register(client)
    await upload_and_ingest(
        client, db_session, storage, headers, data=MILEAGE_HANDBOOK, filename="mileage.txt"
    )
    conversation_id = await start(client, headers)

    await say(client, headers, conversation_id, "What is the mileage reimbursement policy?")
    body = await say(client, headers, conversation_id, "How much is that?")

    assert "45p" in body["assistant_message"]["content"]


async def test_the_query_that_reached_the_index_carries_the_earlier_subject(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The control for the test above, and it took two attempts to write.

    The obvious control — "ask the follow-up alone and assert it is *not*
    answered" — passes for the wrong reason and then fails for a worse one. This
    corpus is small enough to be a single chunk, so retrieval returns it for any
    query at all, and the offline model quotes its most similar sentence. The
    answer contained "45p" either way, which would have made the previous test
    look like proof of something it never touched.

    So the assertion moved to the mechanism: what query actually reached the
    index. That is recorded in the trace, and it is the thing contextualisation
    changes.
    """
    headers = await register(client)
    await upload_and_ingest(
        client, db_session, storage, headers, data=MILEAGE_HANDBOOK, filename="mileage.txt"
    )
    conversation_id = await start(client, headers)

    alone = await say(client, headers, conversation_id, "How much is that?")
    await say(client, headers, conversation_id, "What is the mileage reimbursement policy?")
    follow_up = await say(client, headers, conversation_id, "How much is that?")

    assert "mileage" not in await searched_query(client, headers, alone)
    assert "mileage" in await searched_query(client, headers, follow_up)


async def test_the_run_records_how_much_history_it_was_given(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """Recorded so a run can be replayed with the context it actually had.
    Without it, replaying a follow-up searches for "how much is that?" against an
    empty thread, finds nothing, and looks like a retrieval bug."""
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)
    conversation_id = await start(client, headers)

    await say(client, headers, conversation_id, "How are expenses reimbursed?")
    body = await say(client, headers, conversation_id, "And holiday?")

    run = await db_session.get(AgentRun, uuid.UUID(body["assistant_message"]["agent_run_id"]))
    assert run is not None
    assert run.input["history_turns"] == 2


# --------------------------------------------------------------------------
# the extraction job — ADR-0008
# --------------------------------------------------------------------------


async def test_extraction_is_enqueued_only_after_the_commit(
    client: AsyncClient,
    db_session: AsyncSession,
    storage: ObjectStorage,
    queue: RecordingQueue,
    lifecycle_events: list[str],
) -> None:
    """ADR-0008, and it bites harder here than it did at M5.

    The worker loads the *conversation* from Postgres. A job delivered before the
    commit finds the thread without the exchange it exists to learn from, and
    succeeds having extracted nothing — no error, no retry, nothing to notice.
    """
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)
    conversation_id = await start(client, headers)
    lifecycle_events.clear()

    await say(client, headers, conversation_id, "How are expenses reimbursed?")

    assert [job["function"] for job in queue.jobs].count(EXTRACT_MEMORIES) == 1
    # A commit precedes the enqueue. There is another commit *after* it — the one
    # `get_db` performs on a successful request — so asserting the enqueue is
    # last would be testing the dependency teardown order rather than ADR-0008.
    assert "commit" in lifecycle_events
    assert lifecycle_events.index("commit") < lifecycle_events.index("enqueue")


async def test_the_job_carries_the_user_whose_memories_these_are(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage, queue: RecordingQueue
) -> None:
    """`user_id` travels even though the worker could read it off the
    conversation. It is what every stored memory's *scope* is set from, and
    re-deriving a privacy boundary in a second place is how the two eventually
    disagree."""
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)
    conversation_id = await start(client, headers)

    await say(client, headers, conversation_id, "How are expenses reimbursed?")

    job = next(job for job in queue.jobs if job["function"] == EXTRACT_MEMORIES)
    assert job["conversation_id"] == conversation_id
    assert job["user_id"] is not None


async def test_a_durable_task_row_records_the_work(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """Redis holds the queue; Postgres holds the truth. This row is what a
    sweeper re-enqueues from when Redis was unreachable."""
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)
    conversation_id = await start(client, headers)

    await say(client, headers, conversation_id, "How are expenses reimbursed?")

    task = await db_session.scalar(select(Task).where(Task.kind == EXTRACT_MEMORIES))
    assert task is not None
    assert task.status is TaskStatus.QUEUED
    assert task.payload["conversation_id"] == conversation_id


# --------------------------------------------------------------------------
# tenancy and listing
# --------------------------------------------------------------------------


async def test_another_tenants_thread_is_not_found(client: AsyncClient) -> None:
    """404, not 403. A distinct status turns this into an oracle for enumerating
    conversation ids across organizations."""
    ours = await register(client)
    theirs = await register(client)
    conversation_id = await start(client, theirs)

    response = await client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=ours)

    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_sending_to_another_tenants_thread_is_refused(client: AsyncClient) -> None:
    ours = await register(client)
    theirs = await register(client)
    conversation_id = await start(client, theirs)

    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=ours,
        json={"content": "hello"},
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_listing_returns_your_own_threads_newest_first(client: AsyncClient) -> None:
    headers = await register(client)
    first = await start(client, headers)
    second = await start(client, headers)

    response = await client.get("/api/v1/conversations", headers=headers)

    body = response.json()
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == [second, first]


async def test_an_empty_message_is_rejected(client: AsyncClient) -> None:
    """A bound at the boundary, so a caller gets 422 rather than the model being
    asked to answer nothing."""
    headers = await register(client)
    conversation_id = await start(client, headers)

    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages", headers=headers, json={"content": ""}
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_a_conversation_requires_authentication(client: AsyncClient) -> None:
    response = await client.post("/api/v1/conversations", json={})

    assert response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}
