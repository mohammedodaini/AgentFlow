"""Vector search against real pgvector (M6).

Integration, not unit, and emphatically not mocked. Everything worth asserting
here is enforced by PostgreSQL: the `<=>` operator's ordering, the tenancy
join, and the unique constraint that makes re-indexing safe. A fake vector
store would confirm all three and prove none of them.

The embedder is the real offline one. It matches words rather than meaning,
which is a genuine limitation — and enough that these tests can assert the
*correct* chunk ranked first, rather than that some row came back.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentChunk
from app.rag.embeddings import EmbeddingProvider
from app.rag.retrieval import Retriever
from app.repositories.chunk_repository import MAX_TOP_K, ChunkRepository
from tests.factories import make_document, make_org_with_owner

EXPENSES = "Expenses are reimbursed monthly provided a receipt is attached."
HOLIDAY = "Holiday requests must be approved by a line manager two weeks ahead."
PLANTS = "The office plants are watered every Tuesday by the facilities team."


async def index(
    session: AsyncSession,
    embedder: EmbeddingProvider,
    document: Document,
    texts: list[str],
) -> list[DocumentChunk]:
    """Embed and store `texts` as this document's chunks."""
    vectors = await embedder.embed_documents(texts)

    chunks = [
        DocumentChunk(
            document_id=document.id,
            chunk_index=position,
            content=content,
            token_count=len(content.split()),
            embedding=vector,
        )
        for position, (content, vector) in enumerate(zip(texts, vectors, strict=True))
    ]

    await ChunkRepository(session).replace_for_document(document.id, chunks)
    return chunks


# --------------------------------------------------------------------------
# ranking
# --------------------------------------------------------------------------


async def test_the_most_relevant_chunk_ranks_first(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The whole point of the milestone, asserted end to end through SQL."""
    organization, _, _ = await make_org_with_owner(db_session)
    document = await make_document(db_session, organization=organization, title="handbook.pdf")
    await index(db_session, embedder, document, [EXPENSES, HOLIDAY, PLANTS])

    results = await Retriever(db_session, embedder).retrieve(
        organization.id, "expense receipt reimbursed", top_k=3
    )

    assert results
    assert results[0].content == EXPENSES
    assert [result.score for result in results] == sorted(
        (result.score for result in results), reverse=True
    ), "results must come back in descending relevance"


async def test_results_carry_their_citation(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """A result the user cannot trace to a source is an assertion, not
    evidence — and at M7 the citation is the only thing separating a summary
    from a plausible invention."""
    organization, _, _ = await make_org_with_owner(db_session)
    document = await make_document(db_session, organization=organization, title="handbook.pdf")
    await index(db_session, embedder, document, [EXPENSES])

    results = await Retriever(db_session, embedder).retrieve(organization.id, "expenses")

    assert results[0].document_id == document.id
    assert results[0].document_title == "handbook.pdf"
    assert results[0].chunk_index == 0


async def test_an_exact_match_scores_near_one(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Confirms the distance-to-similarity conversion is the right way round.

    Inverting it is a one-character mistake that ranks the *worst* matches
    first, and every result still looks superficially plausible.
    """
    organization, _, _ = await make_org_with_owner(db_session)
    document = await make_document(db_session, organization=organization)
    await index(db_session, embedder, document, [EXPENSES, PLANTS])

    results = await Retriever(db_session, embedder).retrieve(organization.id, EXPENSES)

    assert results[0].score == pytest.approx(1.0, abs=1e-6)
    assert 0.0 <= results[-1].score <= 1.0


async def test_a_relevance_floor_drops_weak_matches(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Off by default, and available — the distinction M8 will need.

    A floor obviously improves a demo, which is exactly why the default is
    zero: the right threshold depends on the embedding model, and picking one
    by eye silently returns nothing for questions whose answer sits just under
    the line. A user reading "no results" cannot tell that from "we found it
    and hid it". This test pins the mechanism so M8 can set the number from
    measurements rather than build it under deadline.
    """
    organization, _, _ = await make_org_with_owner(db_session)
    document = await make_document(db_session, organization=organization)
    await index(db_session, embedder, document, [EXPENSES, PLANTS])

    unfiltered = await Retriever(db_session, embedder).retrieve(organization.id, EXPENSES)
    filtered = await Retriever(db_session, embedder).retrieve(
        organization.id, EXPENSES, min_score=0.99
    )

    assert len(unfiltered) == 2, "both chunks match to some degree — that is the point"
    assert [result.content for result in filtered] == [EXPENSES]


async def test_top_k_limits_the_result_count(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    organization, _, _ = await make_org_with_owner(db_session)
    document = await make_document(db_session, organization=organization)
    await index(db_session, embedder, document, [EXPENSES, HOLIDAY, PLANTS])

    results = await Retriever(db_session, embedder).retrieve(organization.id, "office", top_k=2)

    assert len(results) == 2


async def test_an_empty_query_returns_nothing_without_embedding_it(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """A vector built from no words matches arbitrarily, so the alternative is
    five confident, unrelated results for a query nobody made."""
    organization, _, _ = await make_org_with_owner(db_session)
    document = await make_document(db_session, organization=organization)
    await index(db_session, embedder, document, [EXPENSES])

    assert await Retriever(db_session, embedder).retrieve(organization.id, "   ") == []


@pytest.mark.parametrize("top_k", [0, -1, MAX_TOP_K + 1])
async def test_an_out_of_range_top_k_is_refused(
    db_session: AsyncSession, embedder: EmbeddingProvider, top_k: int
) -> None:
    """Unbounded `top_k` turns one request into a table scan, and every
    returned chunk is context somebody pays for at M7."""
    organization, _, _ = await make_org_with_owner(db_session)

    with pytest.raises(ValueError, match="top_k"):
        await ChunkRepository(db_session).similarity_search(
            organization.id, await embedder.embed_query("x"), top_k=top_k
        )


# --------------------------------------------------------------------------
# tenancy — the failure that matters most
# --------------------------------------------------------------------------


async def test_another_organizations_chunks_are_never_returned(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The worst possible failure in this system, asserted directly.

    `document_chunks` has no `organization_id`; the scope comes from a join to
    `documents`. Dropping that join would not raise — it would return another
    customer's documents, helpfully ranked by relevance.

    The other tenant's chunk is a *better* match for the query than our own, so
    a broken filter cannot hide behind ranking: it would come back first.
    """
    mine, _, _ = await make_org_with_owner(db_session)
    theirs, _, _ = await make_org_with_owner(db_session)

    my_document = await make_document(db_session, organization=mine)
    their_document = await make_document(db_session, organization=theirs)

    await index(db_session, embedder, my_document, [PLANTS])
    await index(db_session, embedder, their_document, [EXPENSES])

    results = await Retriever(db_session, embedder).retrieve(
        mine.id, "expenses receipt reimbursed", top_k=10
    )

    assert all(result.document_id == my_document.id for result in results)
    assert all(EXPENSES not in result.content for result in results)


async def test_a_tenant_with_no_documents_gets_an_empty_result(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Not an error, and not somebody else's chunks."""
    mine, _, _ = await make_org_with_owner(db_session)
    theirs, _, _ = await make_org_with_owner(db_session)
    their_document = await make_document(db_session, organization=theirs)
    await index(db_session, embedder, their_document, [EXPENSES, HOLIDAY, PLANTS])

    assert await Retriever(db_session, embedder).retrieve(mine.id, "expenses") == []


# --------------------------------------------------------------------------
# re-indexing
# --------------------------------------------------------------------------


async def test_re_indexing_replaces_chunks_instead_of_duplicating_them(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The idempotency guarantee the ingestion task depends on.

    arq can deliver a job twice. Appending would double every chunk, and
    duplicates do not announce themselves — they quietly take two of the top
    five slots in every search that matches them.
    """
    organization, _, _ = await make_org_with_owner(db_session)
    document = await make_document(db_session, organization=organization)
    chunks = ChunkRepository(db_session)

    await index(db_session, embedder, document, [EXPENSES, HOLIDAY])
    await index(db_session, embedder, document, [EXPENSES, HOLIDAY])

    assert await chunks.count_for_document(document.id) == 2

    results = await Retriever(db_session, embedder).retrieve(organization.id, "expenses", top_k=10)
    assert len([result for result in results if result.content == EXPENSES]) == 1


async def test_re_indexing_with_fewer_chunks_removes_the_extras(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Editing a document down must not leave its old tail searchable —
    otherwise a deleted paragraph keeps answering questions."""
    organization, _, _ = await make_org_with_owner(db_session)
    document = await make_document(db_session, organization=organization)

    await index(db_session, embedder, document, [EXPENSES, HOLIDAY, PLANTS])
    await index(db_session, embedder, document, [EXPENSES])

    assert await ChunkRepository(db_session).count_for_document(document.id) == 1


async def test_deleting_a_document_takes_its_chunks(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """`ON DELETE CASCADE`, enforced by PostgreSQL rather than by a Python loop
    that can be interrupted halfway."""
    organization, _, _ = await make_org_with_owner(db_session)
    document = await make_document(db_session, organization=organization)
    await index(db_session, embedder, document, [EXPENSES, HOLIDAY])

    await db_session.delete(document)
    await db_session.flush()

    assert await ChunkRepository(db_session).count_for_document(document.id) == 0


async def test_chunk_positions_are_unique_within_a_document(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The constraint that makes an interrupted re-index fail loudly rather
    than leaving a document with two chunk 0s."""
    organization, _, _ = await make_org_with_owner(db_session)
    document = await make_document(db_session, organization=organization)
    vector = await embedder.embed_query(EXPENSES)

    db_session.add_all(
        [
            DocumentChunk(
                document_id=document.id,
                chunk_index=0,
                content=text,
                token_count=5,
                embedding=vector,
            )
            for text in (EXPENSES, HOLIDAY)
        ]
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()

    await db_session.rollback()


async def test_searching_an_unknown_organization_is_empty_not_an_error(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """An organization id that matches nothing is a normal, boring result."""
    assert await Retriever(db_session, embedder).retrieve(uuid.uuid4(), "expenses") == []
