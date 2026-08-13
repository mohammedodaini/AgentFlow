"""Question → grounded answer with citations. The M7 milestone in one class.

Layer: rag. Four steps, and the value is almost entirely in their order and in
the refusals rather than in any one of them:

    retrieve → assemble within a budget → render the prompt → generate

Thin on purpose. The interesting logic lives where it can be tested without a
model: ranking in `chunk_repository.py`, budgeting in `context.py`, the rules in
`prompts/rag/`. This class is the seam that puts them in the right order, so
`/ask` (M7), the eval harness (M8) and the agent's answer tool (M9) each make
one call rather than keeping three copies of the sequence.

The rule that governs everything here
-------------------------------------
**No context, no call.** When retrieval finds nothing, this refuses locally and
never reaches the model. That is not an optimisation. A model asked a question
with an empty context block answers it from training data — fluently,
confidently, with no citations and no indication that the corpus was silent.
The refusal is the product working correctly; the invented answer is the worst
failure a RAG system has.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm.base import LLMProvider
from app.llm.offline import NO_CONTEXT_ANSWER
from app.prompts import loader as prompts
from app.rag.context import AssembledContext, Source, assemble_context
from app.rag.embeddings import EmbeddingProvider
from app.rag.retrieval import Retriever

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = "rag/system"
ANSWER_PROMPT = "rag/answer"

MIN_EVIDENCE_SCORE = 1e-9
"""Chunks with literally zero similarity are not evidence and are not sent.

This is **not** the tuned relevance threshold M6 deferred to M8, and the
distinction matters. Picking a threshold by eye means silently hiding answers
that sit just under an arbitrary line. This excludes only the degenerate case:
a chunk with *no* measurable similarity to the question.

It was found at runtime, not in a test. A vector search always returns its
`top_k` nearest neighbours however far away they are, so a question the corpus
cannot answer still produced a full context — and `/ask` replied "I could not
find anything about that in your documents" with three citations attached. A
refusal carrying evidence is incoherent: it tells the user both that we found
nothing and that here are the things we found.
"""


@dataclass(frozen=True)
class Answer:
    """A generated answer and everything needed to audit it.

    The metadata is not padding. `sources` is what makes the answer checkable;
    the token counts are what makes the bill explicable (M12); `truncated` is
    the only way anyone learns that a complete-looking answer stopped early; and
    `dropped_sources` says the budget bound this particular question.

    An answer without its sources is an assertion. Returning them is the whole
    reason retrieval was built a milestone before generation.
    """

    text: str
    sources: list[Source] = field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    truncated: bool = False
    context_tokens: int = 0
    dropped_sources: int = 0

    @property
    def is_refusal(self) -> bool:
        """True when nothing was retrieved, so nothing was asked of the model."""
        return not self.sources


class Generator:
    """Answers a question from one organization's documents.

    Takes its collaborators rather than building them, for the reasons
    established at M6: the session belongs to the request, and the embedder and
    the model each own an HTTP client built once per process.
    """

    def __init__(
        self,
        session: AsyncSession,
        embedder: EmbeddingProvider,
        llm: LLMProvider,
        settings: Settings,
    ) -> None:
        self._retriever = Retriever(session, embedder)
        self._llm = llm
        self._settings = settings

    async def answer(
        self, organization_id: uuid.UUID, question: str, *, top_k: int | None = None
    ) -> Answer:
        """Retrieve, ground, and generate."""
        context = await self._context_for(organization_id, question, top_k=top_k)

        if context.is_empty:
            logger.info("generation.refused_no_context", organization_id=str(organization_id))
            return Answer(text=NO_CONTEXT_ANSWER)

        completion = await self._llm.complete(
            system=prompts.load_prompt(SYSTEM_PROMPT),
            prompt=prompts.render(ANSWER_PROMPT, context=context.text, question=question),
        )

        logger.info(
            "generation.answered",
            organization_id=str(organization_id),
            model=self._llm.model,
            sources=len(context.sources),
            context_tokens=context.tokens,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            truncated=completion.was_truncated,
        )

        return Answer(
            text=completion.text,
            sources=context.sources,
            model=self._llm.model,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            truncated=completion.was_truncated,
            context_tokens=context.tokens,
            dropped_sources=context.dropped,
        )

    async def stream_answer(
        self, organization_id: uuid.UUID, question: str, *, top_k: int | None = None
    ) -> tuple[list[Source], AsyncIterator[str]]:
        """The same thing, incrementally.

        Returns the sources *before* the iterator, and that ordering is the
        design. Retrieval finishes before the first token exists, so a client
        can render its citations immediately and fill in the prose as it
        arrives — rather than showing a complete answer whose evidence only
        appears in a trailing event.

        It also keeps the refusal path a normal answer: with no sources there is
        nothing to stream, and the iterator yields the refusal once.
        """
        context = await self._context_for(organization_id, question, top_k=top_k)

        if context.is_empty:
            logger.info("generation.refused_no_context", organization_id=str(organization_id))
            return [], _one(NO_CONTEXT_ANSWER)

        system = prompts.load_prompt(SYSTEM_PROMPT)
        prompt = prompts.render(ANSWER_PROMPT, context=context.text, question=question)

        logger.info(
            "generation.streaming",
            organization_id=str(organization_id),
            model=self._llm.model,
            sources=len(context.sources),
            context_tokens=context.tokens,
        )

        return context.sources, self._llm.stream(system=system, prompt=prompt)

    async def _context_for(
        self, organization_id: uuid.UUID, question: str, *, top_k: int | None
    ) -> AssembledContext:
        results = await self._retriever.retrieve(
            organization_id,
            question,
            top_k=top_k or self._settings.retrieval_top_k,
            min_score=MIN_EVIDENCE_SCORE,
        )
        context = assemble_context(results, budget=self._settings.context_token_budget)

        if context.dropped:
            # Its own log line: it means the budget is binding, which is
            # invisible in the answer and is exactly the number an operator
            # needs before deciding whether to raise it.
            logger.info(
                "generation.context_truncated",
                dropped=context.dropped,
                kept=len(context.sources),
                budget=self._settings.context_token_budget,
            )

        return context


async def _one(text: str) -> AsyncIterator[str]:
    """A single-item async iterator, so the refusal path has the same type as
    the streaming one and the route needs no branch."""
    yield text
