"""The RAG agent graph. The first agent (single-agent milestone).

Layer: agents. Invoked via `services/agent_service.py`, never directly from a
route — the service owns the run row, the trace and the transaction, and a route
calling this directly would produce an untraced run.

The shape
---------
    prepare ──► retrieve ──► enough? ──yes──► generate ──► END
                    ▲            │
                    └──rewrite◄──no (and attempts remain)

A real graph with a real cycle, and the cycle is the point: the failure this
agent exists to beat is a question whose wording shares no vocabulary with the
document that answers it. `/ask` (M7) gets one attempt and gives up. This one
notices it retrieved nothing, rewrites the query, and tries again.

`prepare` arrived at M10 and is what makes this usable in a conversation. It
does the two things that must happen *before* anything is searched:

- **recall** what is already known about the person asking, and
- **contextualise** the question, because "how much is it?" is not a searchable
  string. Retrieval sees one query, not a thread, so a follow-up has to be made
  to stand on its own before it reaches the index — otherwise the best retrieval
  system in the world is being handed three words with no subject.

What this is not, stated plainly
--------------------------------
**It is not a tool-calling ReAct loop.** The model is not choosing which tool to
call — the graph's edges decide, and a node invokes the tool. That is a smaller
claim than "agent" usually implies, and it is the honest one available here:
`LLMProvider` is a text-in/text-out seam (ADR-0010), the offline provider cannot
emit tool calls at all, and there is no API key in this environment to exercise
a model that can.

The structure is built so that changing this is a change to *one node*: the
tools are real `BaseTool`s with real descriptions, ready to bind to a model that
supports tool calling, and `agent_steps` already records `tool_name` and
`tool_input` per step.

**The rewrite and the contextualisation are both deterministic**, a deliberate
application of `docs/agents.md`'s "if code can do it, code does it — LLM calls
are for judgment, not plumbing". Dropping stopwords to turn a sentence into
keywords is plumbing. A model-written *standalone question* is the production
answer to contextualisation and would very likely beat this one; whether it does
is a question M8's harness can now ask, and answering it needs a key.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from typing import Any

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.history import HistoryTurn, select_history
from app.agents.rag.tools import SEARCH_CHUNKS, build_search_chunks
from app.agents.state import AgentState
from app.core.config import Settings
from app.llm.base import LLMProvider
from app.llm.offline import NO_CONTEXT_ANSWER
from app.memory.recall import Recaller
from app.prompts import loader as prompts
from app.rag.context import assemble_context
from app.rag.embeddings import EmbeddingProvider
from app.rag.generation import ANSWER_PROMPT, SYSTEM_PROMPT
from app.repositories.chunk_repository import ScoredChunk

logger = structlog.get_logger(__name__)

PREPARE = "prepare"
RETRIEVE = "retrieve"
REWRITE = "rewrite"
GENERATE = "generate"

CONVERSATIONAL_SYSTEM_PROMPT = "rag/conversational_system"
CONVERSATIONAL_ANSWER_PROMPT = "rag/conversational_answer"
"""The conversational prompt pair, used when a run has history or memories.

A near-copy of `rag/system` and `rag/answer` rather than an extension of them,
and the duplication is deliberate. M8 committed a baseline measured against
those two templates, and ADR-0011 says no prompt change ships without the
harness confirming no regression. Adding two placeholders to the measured
template would change every `/ask` prompt — by whitespace at minimum — and there
is no API key here to re-measure with. So the measured path is left exactly as
it was, and the new path is new.

The cost is real: two files that must be edited together. It is the smaller
cost. Consolidating them is the first thing to do once the harness covers
conversations.
"""

MEMORY_HEADER = "What you remember about this person:"

MAX_ATTEMPTS = 2
"""Retrieval runs at most twice: once as asked, once rewritten.

A bound, not a tuning parameter. A conditional edge that can route back to
`retrieve` is a cycle, and a cycle whose exit depends on a model's output is a
graph that can bill indefinitely. Two, because the second attempt holds nearly
all of the benefit — the third rephrasing of a query that has already failed
twice is almost never the one that works — and every extra attempt is latency a
user waits through.
"""

CONTEXT_TURNS = 2
"""How many earlier user turns contribute keywords to a follow-up query.

Two, because a pronoun almost always refers to the immediately preceding
exchange, and every extra turn adds words that dilute the query. This bound is
the whole reason contextualisation is not simply "prepend the conversation": a
query built from twenty turns matches the *thread's* vocabulary rather than the
question's, and returns whatever the thread has discussed most.
"""

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    [
        "a", "about", "an", "and", "any", "are", "as", "at", "be", "by", "can", "could", "do",
        "does", "for", "from", "get", "have", "how", "i", "if", "in", "is", "it", "long", "many",
        "me", "much", "my", "need", "of", "on", "or", "our", "should", "that", "the", "there",
        "this", "to", "was", "we", "what", "when", "where", "which", "who", "will", "with",
        "would", "you", "your",
    ]
)  # fmt: skip

StepRecorder = Callable[[dict[str, Any]], None]
"""Called once per node with what that node did.

A plain callback rather than a LangGraph callback handler, because what is being
recorded is *our* semantics — which tool ran, what it returned, what it cost —
and a framework hook would hand us framework events to translate. The service
passes one in and persists what it collects.
"""


def build_graph(
    session: AsyncSession,
    embedder: EmbeddingProvider,
    llm: LLMProvider,
    settings: Settings,
    organization_id: uuid.UUID,
    record: StepRecorder,
) -> CompiledStateGraph[AgentState, Any, AgentState, AgentState]:
    """Compile a RAG graph bound to one request.

    Built per request rather than once per process, because it closes over the
    session and the tenant — the same reasoning as `build_search_chunks`. The
    cost is negligible: compiling a four-node graph takes microseconds, while a
    shared graph holding a request's session would be a correctness bug rather
    than an optimisation.
    """
    search = build_search_chunks(session, embedder, organization_id)
    recaller = Recaller(session, embedder)

    async def prepare(state: AgentState) -> AgentState:
        """Recall what is known, and make the question searchable on its own.

        Runs on every run, including one-shot ones with no history. A step
        reporting `{"memories": [], "context_terms": ""}` costs one row and
        answers "did it recall nothing, or never look?" — different bugs with
        identical symptoms.

        Recall is scoped to the authenticated user and the closed-over tenant,
        never to anything the model chooses (ADR-0012).
        """
        started = time.perf_counter()
        question = state["question"]

        memories = await recaller.recall(
            organization_id, question, user_id=state.get("user_id"), top_k=settings.memory_top_k
        )

        turns = [HistoryTurn(**turn) for turn in state.get("history", [])]
        terms = _context_terms(turns)
        # The original question is kept whole and the terms prepended, rather
        # than the two being merged into one keyword bag. The question's own
        # phrasing is the strongest signal available; the terms exist only to
        # give a pronoun something to point at.
        query = f"{terms} {question}".strip() if terms else question

        record(
            {
                "node_name": PREPARE,
                "tool_name": None,
                "tool_input": {"history_turns": len(turns)},
                # Memory *contents* are recorded, unlike chunk contents. They are
                # one sentence each and capped at a handful, so the trace stays
                # small — and a wrong answer caused by a wrong memory is
                # undiagnosable without knowing which memory it was.
                "tool_output": {
                    "memories": [memory.content for memory in memories],
                    "context_terms": terms,
                },
                "latency_ms": _elapsed_ms(started),
                "tokens": 0,
            }
        )

        return {
            "search_query": query,
            "context_terms": terms,
            "memories": [
                {
                    "memory_id": str(memory.memory_id),
                    "content": memory.content,
                    "scope": memory.scope.value,
                    "score": memory.score,
                }
                for memory in memories
            ],
        }

    async def retrieve(state: AgentState) -> AgentState:
        """Ask the search tool for passages."""
        query = state.get("search_query") or state["question"]
        started = time.perf_counter()

        chunks: list[dict[str, Any]] = await search.ainvoke(
            {"query": query, "top_k": settings.retrieval_top_k}
        )

        record(
            {
                "node_name": RETRIEVE,
                "tool_name": SEARCH_CHUNKS,
                "tool_input": {"query": query, "top_k": settings.retrieval_top_k},
                # The chunk *text* is deliberately not stored in the trace. It is
                # already in `document_chunks`, it is the largest thing here, and
                # copying a corpus into a trace table is how a trace table
                # becomes the biggest one in the database.
                "tool_output": {
                    "count": len(chunks),
                    "document_ids": sorted({chunk["document_id"] for chunk in chunks}),
                    "top_score": chunks[0]["score"] if chunks else 0.0,
                },
                "latency_ms": _elapsed_ms(started),
                "tokens": 0,
            }
        )

        return {
            "search_query": query,
            "retrieved": chunks,
            "attempts": state.get("attempts", 0) + 1,
        }

    async def rewrite(state: AgentState) -> AgentState:
        """Turn the question into keywords and try again.

        Deterministic: stopwords out, the rest kept. It helps precisely where the
        first attempt fails — a conversational question ("How much notice do I
        need to give for holiday?") carries more filler than content, and the
        filler is what a lexical embedder matched on.

        Rebuilt from `question` plus `context_terms`, never from `search_query`.
        Rewriting an already-rewritten string compounds: the second attempt would
        either strip stopwords from a keyword list, changing nothing, or strip
        them from a contextualised query, quietly discarding the original
        question's structure while keeping the borrowed terms.

        If stripping leaves nothing, the original is kept rather than searching
        for an empty string, which matches arbitrarily.
        """
        started = time.perf_counter()
        keywords = " ".join(_keywords(state["question"]))
        terms = state.get("context_terms", "")
        rewritten = f"{terms} {keywords}".strip() or state["question"]

        record(
            {
                "node_name": REWRITE,
                "tool_name": None,
                "tool_input": {"question": state["question"]},
                "tool_output": {"search_query": rewritten},
                "latency_ms": _elapsed_ms(started),
                "tokens": 0,
            }
        )

        return {"search_query": rewritten}

    async def generate(state: AgentState) -> AgentState:
        """Ground the answer in whatever was retrieved.

        The same rule as `/ask` (ADR-0010), enforced the same way: with no usable
        context the model is never called. A graph that reached a model with an
        empty context block would produce a fluent invention — and, worse than at
        `/ask`, one with a trace showing that the agent "worked".

        **Memory and history do not soften that rule.** A run that recalled three
        memories and retrieved no documents still refuses. Memories are not
        evidence: they are uncited assertions from an earlier conversation, and
        answering from them would produce exactly the confident, unverifiable
        reply the refusal exists to prevent.
        """
        started = time.perf_counter()
        context = assemble_context(
            [_as_chunk(chunk) for chunk in state.get("retrieved", [])],
            budget=settings.context_token_budget,
        )

        if context.is_empty:
            record(
                {
                    "node_name": GENERATE,
                    "tool_name": None,
                    "tool_input": {"sources": 0},
                    "tool_output": {"refused": True},
                    "latency_ms": _elapsed_ms(started),
                    "tokens": 0,
                }
            )
            return {
                "answer": NO_CONTEXT_ANSWER,
                "citations": [],
                "usage": {"input": 0, "output": 0},
            }

        turns = [HistoryTurn(**turn) for turn in state.get("history", [])]
        history = select_history(turns, budget=settings.history_token_budget) if turns else None
        memories = _render_memories(state.get("memories", []))
        conversational = bool(history and not history.is_empty) or bool(memories)

        system, prompt = _build_prompt(
            question=state["question"],
            context=context.text,
            history=history.text if history else "",
            memories=memories,
            conversational=conversational,
        )
        completion = await llm.complete(system=system, prompt=prompt)

        record(
            {
                "node_name": GENERATE,
                "tool_name": None,
                "tool_input": {
                    "sources": len(context.sources),
                    "context_tokens": context.tokens,
                    "history_tokens": history.tokens if history else 0,
                    "history_dropped": history.dropped if history else 0,
                    "conversational": conversational,
                },
                "tool_output": {"truncated": completion.was_truncated},
                "latency_ms": _elapsed_ms(started),
                "tokens": completion.input_tokens + completion.output_tokens,
            }
        )

        return {
            "answer": completion.text,
            "citations": [
                {
                    "number": source.number,
                    "chunk_id": source.chunk_id,
                    "document_id": source.document_id,
                    "document_title": source.document_title,
                    "chunk_index": source.chunk_index,
                    "score": source.score,
                }
                for source in context.sources
            ],
            "usage": {"input": completion.input_tokens, "output": completion.output_tokens},
        }

    graph: StateGraph[AgentState, Any, AgentState, AgentState] = StateGraph(AgentState)
    graph.add_node(PREPARE, prepare)
    graph.add_node(RETRIEVE, retrieve)
    graph.add_node(REWRITE, rewrite)
    graph.add_node(GENERATE, generate)

    graph.add_edge(START, PREPARE)
    graph.add_edge(PREPARE, RETRIEVE)
    graph.add_conditional_edges(RETRIEVE, _enough, {GENERATE: GENERATE, REWRITE: REWRITE})
    graph.add_edge(REWRITE, RETRIEVE)
    graph.add_edge(GENERATE, END)

    return graph.compile()


def _enough(state: AgentState) -> str:
    """The conditional edge: answer now, or rewrite and search again.

    Retrying only on an *empty* result, never a weak one. M8 measured why: the
    score distributions of answerable and unanswerable questions overlap
    outright, so "weak" cannot be told apart from "correct but low-scoring", and
    retrying on a threshold would spend a second retrieval on most successful
    questions to rescue almost none.

    At module level rather than inside `build_graph`, because it closes over
    nothing — it is a pure function of state, and it is the piece of routing
    logic most worth testing without a database behind it.
    """
    if state.get("retrieved"):
        return GENERATE

    if state.get("attempts", 0) >= MAX_ATTEMPTS:
        # Out of attempts with nothing found. `generate` refuses; that is not a
        # failure, and routing to END here would return an empty answer with no
        # explanation instead of an honest refusal.
        return GENERATE

    return REWRITE


def _build_prompt(
    *, question: str, context: str, history: str, memories: str, conversational: bool
) -> tuple[str, str]:
    """Pick the prompt pair and render it. Returns `(system, prompt)`.

    A module-level function rather than a branch inside the `generate` closure,
    so the choice of template is testable without building a graph, a session and
    an embedder — the one thing here most likely to be got wrong silently, since
    both branches produce a perfectly plausible prompt.
    """
    if not conversational:
        return (
            prompts.load_prompt(SYSTEM_PROMPT),
            prompts.render(ANSWER_PROMPT, context=context, question=question),
        )

    return (
        prompts.load_prompt(CONVERSATIONAL_SYSTEM_PROMPT),
        prompts.render(
            CONVERSATIONAL_ANSWER_PROMPT,
            memories=memories,
            history=history,
            context=context,
            question=question,
        ),
    )


def _keywords(text: str) -> list[str]:
    """Content words, in order, with stopwords removed."""
    return [word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS]


def _context_terms(turns: list[HistoryTurn]) -> str:
    """Keywords borrowed from the most recent user turns.

    Only *user* turns. An assistant reply is mostly quoted source text, so
    borrowing from it would feed the previous answer's vocabulary back into the
    next search — a loop that narrows every follow-up onto whatever was found
    first, whether or not it was right.

    Deduplicated while preserving order, so a word repeated across two turns does
    not count twice against a query the embedder treats as a bag of words.
    """
    recent = [turn for turn in turns if turn.role == "user"][-CONTEXT_TURNS:]
    seen: dict[str, None] = {}

    for turn in recent:
        for word in _keywords(turn.content):
            seen.setdefault(word, None)

    return " ".join(seen)


def _render_memories(memories: list[dict[str, Any]]) -> str:
    """The memory block, or an empty string when nothing was recalled.

    Empty rather than a header with no items beneath it: a heading followed by
    nothing reads to a model as "you remember nothing about this person", which
    is a claim rather than an absence.
    """
    if not memories:
        return ""

    lines = "\n".join(f"- {memory['content']}" for memory in memories)
    return f"{MEMORY_HEADER}\n{lines}"


def _as_chunk(payload: dict[str, Any]) -> ScoredChunk:
    """Rebuild a `ScoredChunk` from its serialised form.

    State crosses a JSON boundary — it is checkpointed — so ids come back as
    strings and have to be parsed. Doing that here, rather than keeping objects
    in state, is what keeps the state checkpointable at all.
    """
    return ScoredChunk(
        chunk_id=uuid.UUID(payload["chunk_id"]),
        document_id=uuid.UUID(payload["document_id"]),
        document_title=payload["document_title"],
        chunk_index=payload["chunk_index"],
        content=payload["content"],
        token_count=payload["token_count"],
        score=payload["score"],
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
