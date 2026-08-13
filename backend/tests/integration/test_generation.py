"""The generator, against a real database (M7).

Integration rather than unit, because what is worth asserting is that the four
steps compose: retrieval really ranks, the budget really binds, the prompt
really carries the right chunks, and the citations really point back at them.
Mocking the retriever would test the arrangement of four mocks.

The model is the offline one, which is not a mock — it can only answer if the
right passage reached the prompt. See ADR-0010.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm.base import Completion, LLMError, LLMProvider
from app.llm.offline import NO_CONTEXT_ANSWER, OfflineLLM
from app.rag.embeddings import EmbeddingProvider
from app.rag.generation import Generator
from tests.factories import make_document, make_org_with_owner
from tests.integration.test_retrieval import EXPENSES, HOLIDAY, PLANTS, index


class ExplodingLLM:
    """Fails if it is called at all.

    Used two ways: to prove the refusal path never reaches a model, and to prove
    that a model failure surfaces rather than being swallowed. Both are
    assertions about *whether* the call happened, which no amount of inspecting
    the answer text could make.
    """

    model = "exploding"

    async def complete(self, *, system: str, prompt: str) -> Completion:
        del system, prompt
        message = "the model is down"
        raise LLMError(message)

    async def stream(self, *, system: str, prompt: str) -> AsyncIterator[str]:
        del system, prompt
        message = "the model is down"
        raise LLMError(message)
        yield ""  # pragma: no cover — makes this an async generator


def generator(
    session: AsyncSession,
    embedder: EmbeddingProvider,
    *,
    llm: LLMProvider | None = None,
    **overrides: object,
) -> Generator:
    return Generator(
        session, embedder, llm or OfflineLLM(), get_settings().model_copy(update=overrides)
    )


async def corpus(session: AsyncSession, embedder: EmbeddingProvider) -> uuid.UUID:
    """One organization with a three-paragraph handbook, genuinely indexed."""
    organization, _, _ = await make_org_with_owner(session)
    document = await make_document(session, organization=organization, title="handbook.pdf")
    await index(session, embedder, document, [EXPENSES, HOLIDAY, PLANTS])
    return organization.id


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


async def test_the_answer_is_drawn_from_the_retrieved_passage(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The milestone, end to end through the database.

    Asserting that the answer contains text from the *right* chunk, not merely
    that a string came back. A generator that retrieved the wrong passage would
    still produce fluent output, and only this kind of assertion notices.
    """
    organization_id = await corpus(db_session, embedder)

    answer = await generator(db_session, embedder).answer(
        organization_id, "expenses receipt reimbursed"
    )

    assert "reimbursed" in answer.text
    assert not answer.is_refusal


async def test_citations_point_at_the_chunks_that_were_used(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """An answer whose citations are unverifiable is an assertion.

    The marker in the text and the citation in the metadata have to name the
    same chunk — the failure being guarded is two well-formed halves that
    disagree, which raises nothing.
    """
    organization_id = await corpus(db_session, embedder)

    answer = await generator(db_session, embedder).answer(organization_id, "holiday approval")

    assert answer.sources
    cited = answer.text.rsplit("[", 1)[-1].rstrip("]")
    source = next(item for item in answer.sources if str(item.number) == cited)

    assert source.document_title == "handbook.pdf"
    assert uuid.UUID(source.chunk_id)


async def test_usage_and_model_travel_with_the_answer(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """M12 bills on these. Capturing them from the first call means that
    milestone is a query rather than an archaeology project."""
    organization_id = await corpus(db_session, embedder)

    answer = await generator(db_session, embedder).answer(organization_id, "expenses")

    assert answer.model == "offline-extractive"
    assert answer.input_tokens > 0
    assert answer.context_tokens > 0


# --------------------------------------------------------------------------
# the refusal — the most important behaviour here
# --------------------------------------------------------------------------


async def test_nothing_retrieved_means_the_model_is_never_called(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The worst failure a RAG system has, prevented structurally.

    A model handed an empty context block answers from training data —
    fluently, confidently, with no citations and no sign that the corpus was
    silent. So the refusal happens *before* the call, and this asserts the call
    never happened rather than merely that the text looks like a refusal.
    """
    organization, _, _ = await make_org_with_owner(db_session)

    answer = await generator(db_session, embedder, llm=ExplodingLLM()).answer(
        organization.id, "what is our parental leave policy?"
    )

    assert answer.text == NO_CONTEXT_ANSWER
    assert answer.is_refusal
    assert answer.sources == []


async def test_a_question_the_corpus_cannot_answer_refuses_without_citations(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """A regression test, and the bug was found at runtime rather than here.

    A vector search always returns its `top_k` nearest neighbours, however far
    away they are — "nothing relevant" is not a state pgvector can report. So a
    question the documents cannot answer still produced a full context, the
    model still refused, and `/ask` returned "I could not find anything about
    that in your documents" **with three citations attached**.

    Incoherent, and worse than either half alone: it tells the user we found
    nothing and simultaneously offers the things we found. Every test passed,
    because each half was individually correct.
    """
    organization_id = await corpus(db_session, embedder)

    answer = await generator(db_session, embedder).answer(
        organization_id, "zxqv parental leave entitlement"
    )

    assert answer.is_refusal
    assert answer.sources == [], "a refusal must not carry evidence"
    assert answer.text == NO_CONTEXT_ANSWER


async def test_another_tenants_documents_are_never_summarised(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Worse here than at `/search`, and worth its own test.

    A tenancy leak in retrieval returns another customer's passages. A leak here
    *rewrites them in fluent prose* — harder to spot in a log, and impossible to
    unsee once a user has read it.
    """
    await corpus(db_session, embedder)
    mine, _, _ = await make_org_with_owner(db_session)
    my_document = await make_document(db_session, organization=mine, title="ours.txt")
    await index(db_session, embedder, my_document, ["We ship on Fridays and nothing else."])

    answer = await generator(db_session, embedder).answer(mine.id, "expenses receipt reimbursed")

    assert "reimbursed" not in answer.text
    assert all(source.document_title == "ours.txt" for source in answer.sources)


# --------------------------------------------------------------------------
# the budget
# --------------------------------------------------------------------------


async def test_a_small_budget_drops_sources_and_says_so(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The budget binding is invisible in the answer. `dropped_sources` is the
    only way an operator learns a question was answered from three of eight
    retrieved chunks.

    The query names something from every paragraph on purpose. A narrower one
    would be filtered down to a single chunk by the zero-similarity rule before
    the budget ever applied — so the test would pass for the wrong reason and
    stop covering the budget at all.
    """
    organization_id = await corpus(db_session, embedder)

    answer = await generator(db_session, embedder, context_token_budget=30).answer(
        organization_id, "expenses holiday plants", top_k=3
    )

    assert answer.dropped_sources >= 1
    assert len(answer.sources) < 3
    assert answer.context_tokens <= 30


async def test_a_budget_too_small_for_anything_becomes_a_refusal(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Not a crash, and not an ungrounded answer. If nothing fits there is no
    context, and no context means no call."""
    organization_id = await corpus(db_session, embedder)

    answer = await generator(
        db_session, embedder, llm=ExplodingLLM(), context_token_budget=1
    ).answer(organization_id, "expenses")

    assert answer.is_refusal


# --------------------------------------------------------------------------
# failure
# --------------------------------------------------------------------------


async def test_a_model_failure_propagates_as_llm_error(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """Which `app/api/errors.py` maps to 502 — an upstream failed, not us.
    Swallowing it and returning an empty answer would present an outage as
    "your documents say nothing about that"."""
    organization_id = await corpus(db_session, embedder)

    with pytest.raises(LLMError):
        await generator(db_session, embedder, llm=ExplodingLLM()).answer(
            organization_id, "expenses"
        )


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------


async def test_streaming_returns_sources_before_any_token(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The ordering the whole streaming design rests on.

    Retrieval finishes before the first token exists, so a client can render its
    citations immediately rather than waiting for a trailing event — the
    difference between an answer that arrives with its evidence and one whose
    evidence lands after the reader has finished.
    """
    organization_id = await corpus(db_session, embedder)

    sources, tokens = await generator(db_session, embedder).stream_answer(
        organization_id, "expenses receipt"
    )

    assert sources, "citations must be known before the first token"
    assert "reimbursed" in "".join([piece async for piece in tokens])


async def test_streaming_a_question_with_no_context_yields_the_refusal(
    db_session: AsyncSession, embedder: EmbeddingProvider
) -> None:
    """The refusal path stays a normal answer with no sources, so the route
    needs no branch and the client no special case."""
    organization, _, _ = await make_org_with_owner(db_session)

    sources, tokens = await generator(db_session, embedder, llm=ExplodingLLM()).stream_answer(
        organization.id, "anything"
    )

    assert sources == []
    assert "".join([piece async for piece in tokens]) == NO_CONTEXT_ANSWER


def test_the_corpus_paragraphs_are_the_ones_the_assertions_assume() -> None:
    """Guards the imports above: these three strings come from the M6 retrieval
    tests, and a reword there would silently weaken every assertion here."""
    assert "reimbursed" in EXPENSES
    assert "Holiday" in HOLIDAY
    assert "plants" in PLANTS
