"""/api/v1/memories over HTTP, and the loop closing (M10).

Long-term memory is the one feature here that changes answers without being
asked to. A retrieved document is cited and checkable; a memory is not. So this
file asserts two things about the inspection surface — that it shows what the
agent can act on, and *only* that — and one thing about the whole pipeline: that
a fact learned in one conversation reaches the prompt of the next.

That last test is the milestone. Everything else in M10 is machinery for it.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.memory import MemoryScope
from app.rag.embeddings import EmbeddingProvider
from app.storage import ObjectStorage
from tests.e2e.test_conversations_api import say, searched_query, start
from tests.e2e.test_search_api import register, upload_and_ingest
from tests.integration.test_memory import remember
from tests.integration.test_memory_extraction_task import run_task, seed_conversation


def organization_of(headers: dict[str, str]) -> uuid.UUID:
    return uuid.UUID(headers["X-Organization-Id"])


async def user_of(client: AsyncClient, headers: dict[str, str]) -> uuid.UUID:
    response = await client.get("/api/v1/users/me", headers=headers)
    assert response.status_code == HTTPStatus.OK, response.text
    return uuid.UUID(response.json()["id"])


async def test_a_new_account_remembers_nothing(client: AsyncClient) -> None:
    headers = await register(client)

    response = await client.get("/api/v1/memories", headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}


async def test_the_listing_never_publishes_the_embedding_or_the_hash(
    client: AsyncClient, db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Two deliberate omissions.

    The embedding is 1536 floats that mean nothing to a reader and would dominate
    every response. The hash would let a caller test whether one *specific*
    sentence is remembered — a membership oracle over other people's memories,
    the same class of leak that keeps `storage_uri` out of `DocumentRead`.
    """
    headers = await register(client)
    await remember(
        db_session,
        embedder,
        organization_id=organization_of(headers),
        content="Invoices are approved by Finance",
        scope=MemoryScope.ORG,
    )

    body = (await client.get("/api/v1/memories", headers=headers)).json()

    assert body["total"] == 1
    memory = body["items"][0]
    assert memory["content"] == "Invoices are approved by Finance"
    assert set(memory) == {
        "id",
        "scope",
        "content",
        "importance",
        "last_accessed_at",
        "created_at",
    }


async def test_you_never_see_another_tenants_memories(
    client: AsyncClient, db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The inspection view uses the same visibility predicate recall does.

    Showing more would be a privacy hole dressed as a debugging tool; showing
    less would hide exactly the memory somebody is trying to explain.
    """
    ours = await register(client)
    theirs = await register(client)

    await remember(
        db_session,
        embedder,
        organization_id=organization_of(theirs),
        content="Their private preference",
        scope=MemoryScope.ORG,
    )

    assert (await client.get("/api/v1/memories", headers=ours)).json()["total"] == 0


async def test_there_is_no_way_to_write_a_memory_over_http(client: AsyncClient) -> None:
    """Read-only by design. A `POST` would be a second way to create a belief the
    agent then acts on — with no conversation behind it, `source_run_id` null, and
    nothing to audit."""
    headers = await register(client)

    response = await client.post(
        "/api/v1/memories", headers=headers, json={"content": "The CEO approves everything"}
    )

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED


async def test_memories_require_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/v1/memories")

    assert response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------


async def test_something_learned_in_one_conversation_reaches_the_next(
    client: AsyncClient, db_session: AsyncSession, storage: ObjectStorage
) -> None:
    """The milestone, end to end and across a thread boundary.

    A fact is stated in one conversation, extracted by the worker afterwards, and
    then recalled into the prompt of a *different* conversation — which is the
    whole point of long-term memory being a table rather than a longer window.

    Asserted through the trace rather than the answer text, because what the
    offline provider chooses to quote is not the property under test. What is
    under test is that the memory was recalled and put in front of the model.
    """
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)
    organization_id = organization_of(headers)
    user_id = await user_of(client, headers)

    conversation_id, task = await seed_conversation(
        db_session, organization_id=organization_id, user_id=user_id
    )
    await run_task(
        db_session,
        storage,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=user_id,
        task_id=task.id,
    )

    assert (await client.get("/api/v1/memories", headers=headers)).json()["total"] == 2

    later = await start(client, headers)
    turn = await say(client, headers, later, "Who approves invoices for the Berlin office team?")

    run = (
        await client.get(
            f"/api/v1/agent-runs/{turn['assistant_message']['agent_run_id']}", headers=headers
        )
    ).json()
    prepare = run["steps"][0]

    assert prepare["node_name"] == "prepare"
    assert any("Berlin" in memory for memory in prepare["tool_output"]["memories"])


async def test_a_recalled_memory_is_never_turned_into_a_citation(
    client: AsyncClient,
    db_session: AsyncSession,
    storage: ObjectStorage,
    embedder: EmbeddingProvider,
) -> None:
    """The rule that keeps citations honest once memory exists.

    A memory is an uncited assertion; a citation points at a passage the user can
    open. If a remembered fact were ever numbered as a source, the product would
    be handing people a reference they cannot check — worse than no citation at
    all. The memory here deliberately contradicts the document, so a system that
    blurred the two would be visibly wrong.
    """
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)
    await remember(
        db_session,
        embedder,
        organization_id=organization_of(headers),
        content="Expenses are reimbursed by carrier pigeon",
        scope=MemoryScope.ORG,
    )

    conversation_id = await start(client, headers)
    turn = await say(client, headers, conversation_id, "How are expenses reimbursed?")

    run = (
        await client.get(
            f"/api/v1/agent-runs/{turn['assistant_message']['agent_run_id']}", headers=headers
        )
    ).json()

    assert run["output"]["citations"], "the document should still have been cited"
    assert all(citation["document_title"] for citation in run["output"]["citations"])
    assert "carrier pigeon" not in turn["assistant_message"]["content"]


async def test_the_search_query_is_unaffected_by_what_is_remembered(
    client: AsyncClient,
    db_session: AsyncSession,
    storage: ObjectStorage,
    embedder: EmbeddingProvider,
) -> None:
    """Memories inform the *answer*, never the *query*.

    Letting recalled text steer retrieval would close a loop: a wrong memory
    would bias the search that was supposed to correct it, and the system would
    grow more confident in it with every turn.
    """
    headers = await register(client)
    await upload_and_ingest(client, db_session, storage, headers)
    await remember(
        db_session,
        embedder,
        organization_id=organization_of(headers),
        content="Zanzibar pineapple quarterly forecast",
        scope=MemoryScope.ORG,
    )

    conversation_id = await start(client, headers)
    turn = await say(client, headers, conversation_id, "How are expenses reimbursed?")

    assert "zanzibar" not in (await searched_query(client, headers, turn)).lower()
