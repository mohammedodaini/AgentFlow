"""The conversational prompt path and offline extraction (M10).

Two things are pinned down here, and both are couplings between a template file
and code that parses it — the class of bug that drifts silently and is only ever
noticed by a human reading output that looks fine.

1. **`OfflineLLM` picks extraction mode from a marker in the real template.** If
   `memory/extract.md` stopped ending in `Facts:`, extraction would fall through
   to the *answering* path and try to answer the conversation as though it were a
   question. The result would be fluent, plausible, entirely fabricated "facts" —
   stored, uncited, and steering every later reply.

2. **The conversational template keeps `Question:` last, and puts the memory and
   history blocks above the sources.** `OfflineLLM` finds context blocks by
   matching `[n]` at line start and stops at `Question:`; putting either block
   after the sources would make it parse a remembered fact as a citable source.
"""

from __future__ import annotations

from app.agents.history import HistoryTurn
from app.agents.rag.graph import (
    CONVERSATIONAL_ANSWER_PROMPT,
    CONVERSATIONAL_SYSTEM_PROMPT,
    GENERATE,
    MAX_ATTEMPTS,
    MEMORY_HEADER,
    REWRITE,
    _build_prompt,
    _context_terms,
    _enough,
    _render_memories,
)
from app.agents.state import AgentState
from app.llm.offline import FACTS_LABEL, QUESTION_LABEL, OfflineLLM
from app.memory.writer import EXTRACTION_PROMPT, parse_facts
from app.prompts import loader as prompts

CONTEXT = "[1] handbook.pdf\nExpenses are reimbursed."

TRANSCRIPT = "\n".join(
    [
        "Earlier in this conversation:",
        "User: I work in the Berlin office and I approve invoices for my team.",
        "Assistant: Expenses are reimbursed within 30 days. [1]",
        "User: How long does that take?",
    ]
)


def extraction_prompt(transcript: str = TRANSCRIPT) -> str:
    return prompts.render(EXTRACTION_PROMPT, transcript=transcript)


# --------------------------------------------------------------------------
# the extraction template, and the marker that selects the mode
# --------------------------------------------------------------------------


def test_the_real_template_ends_with_the_marker_offline_llm_looks_for() -> None:
    """The coupling, asserted against the file rather than a copy of it."""
    assert extraction_prompt().rstrip().endswith(FACTS_LABEL)


async def test_extraction_returns_bullets_the_writer_can_parse() -> None:
    """End to end through the two modules that have to agree on a format."""
    completion = await OfflineLLM().complete(system="", prompt=extraction_prompt())

    assert parse_facts(completion.text) == [
        "I work in the Berlin office and I approve invoices for my team"
    ]


async def test_a_question_is_never_stored_as_a_fact() -> None:
    """In a RAG product nearly every user turn is a question. Without this the
    store fills with what people *asked* rather than what they said, and recall
    surfaces old questions as though they were knowledge."""
    completion = await OfflineLLM().complete(system="", prompt=extraction_prompt())

    assert "How long does that take" not in completion.text


async def test_the_assistant_is_never_quoted_back_as_a_fact() -> None:
    """Rule 3 of the extraction prompt, and the failure that makes agent memory
    dangerous rather than merely useless: learning from the system's own guesses
    turns one wrong answer into a permanent belief."""
    completion = await OfflineLLM().complete(system="", prompt=extraction_prompt())

    assert "reimbursed within 30 days" not in completion.text


async def test_a_conversation_with_nothing_durable_extracts_nothing() -> None:
    transcript = "Earlier in this conversation:\nUser: How are expenses reimbursed?"

    completion = await OfflineLLM().complete(system="", prompt=extraction_prompt(transcript))

    assert parse_facts(completion.text) == []


async def test_answering_still_works_and_is_not_confused_for_extraction() -> None:
    """The mode switch must not have broken the path M7 measured."""
    completion = await OfflineLLM().complete(
        system="", prompt=prompts.render("rag/answer", context=CONTEXT, question="Expenses?")
    )

    assert "reimbursed" in completion.text
    assert "[1]" in completion.text


# --------------------------------------------------------------------------
# the conversational answer template
# --------------------------------------------------------------------------


def test_the_question_stays_last_so_the_block_parser_terminates() -> None:
    """Without this the final source block runs to the end of the string and
    swallows the question, which then matches itself perfectly — the M7 bug that
    made the model parrot the question back with a citation attached."""
    rendered = prompts.render(
        CONVERSATIONAL_ANSWER_PROMPT,
        memories="",
        history="",
        context=CONTEXT,
        question="How are expenses reimbursed?",
    )

    assert rendered.strip().splitlines()[-1].startswith(QUESTION_LABEL)


def test_memories_and_history_sit_above_the_sources() -> None:
    """So `[n]` block parsing cannot mistake a remembered fact for a citable
    source. A memory quoted as a source would give an uncited assertion a
    citation number, which is the worst possible outcome for both."""
    rendered = prompts.render(
        CONVERSATIONAL_ANSWER_PROMPT,
        memories=_render_memories([{"content": "Works in Berlin"}]),
        history="Earlier in this conversation:\nUser: hello",
        context=CONTEXT,
        question="And the rate?",
    )

    assert rendered.index("Works in Berlin") < rendered.index("[1]")
    assert rendered.index("User: hello") < rendered.index("[1]")


def test_the_conversational_system_prompt_forbids_citing_a_memory() -> None:
    """Rule 6 is what keeps citations honest once memory exists.

    Whitespace is collapsed before matching, because the template is wrapped for
    humans and an assertion that broke on a re-wrap would be testing the line
    width rather than the rule.
    """
    system = " ".join(prompts.load_prompt(CONVERSATIONAL_SYSTEM_PROMPT).lower().split())

    assert "never cite a remembered fact" in system
    assert "the source wins" in system


def test_a_one_shot_run_uses_the_measured_prompt_pair() -> None:
    """`make eval` measured `rag/answer`. A run with no history and no memories
    must still get exactly that prompt, or the committed baseline stops
    describing anything real (ADR-0011)."""
    question = "How are expenses reimbursed?"

    system, prompt = _build_prompt(
        question=question, context=CONTEXT, history="", memories="", conversational=False
    )

    assert system == prompts.load_prompt("rag/system")
    assert prompt == prompts.render("rag/answer", context=CONTEXT, question=question)


def test_a_conversational_run_uses_the_conversational_pair() -> None:
    system, prompt = _build_prompt(
        question="And the rate?",
        context=CONTEXT,
        history="Earlier in this conversation:\nUser: hello",
        memories=_render_memories([{"content": "Works in Berlin"}]),
        conversational=True,
    )

    assert system == prompts.load_prompt(CONVERSATIONAL_SYSTEM_PROMPT)
    assert "Works in Berlin" in prompt
    assert "User: hello" in prompt


# --------------------------------------------------------------------------
# rendering memories
# --------------------------------------------------------------------------


def test_no_memories_renders_no_header() -> None:
    """A heading with nothing under it reads to a model as "you remember nothing
    about this person" — a claim, rather than an absence."""
    assert _render_memories([]) == ""


def test_memories_render_as_a_labelled_list() -> None:
    rendered = _render_memories([{"content": "Works in Berlin"}, {"content": "Approves invoices"}])

    assert rendered.startswith(MEMORY_HEADER)
    assert "- Works in Berlin" in rendered
    assert "- Approves invoices" in rendered


# --------------------------------------------------------------------------
# contextualising a follow-up
# --------------------------------------------------------------------------


def test_a_follow_up_borrows_keywords_from_the_previous_question() -> None:
    """The failure this exists to beat: retrieval sees one query, not a thread,
    so "how much is it?" is three words with no subject."""
    terms = _context_terms(
        [
            HistoryTurn(role="user", content="What is the mileage reimbursement rate?"),
            HistoryTurn(role="assistant", content="45p per mile. [1]"),
        ]
    )

    assert "mileage" in terms
    assert "reimbursement" in terms


def test_terms_never_come_from_the_assistant() -> None:
    """An assistant reply is mostly quoted source text. Borrowing from it feeds
    the previous answer's vocabulary into the next search — a loop that narrows
    every follow-up onto whatever was found first, right or wrong."""
    terms = _context_terms(
        [HistoryTurn(role="assistant", content="Zanzibar pineapple quarterly forecast")]
    )

    assert terms == ""


def test_stopwords_are_dropped_and_words_are_not_repeated() -> None:
    terms = _context_terms(
        [
            HistoryTurn(role="user", content="What is the holiday policy?"),
            HistoryTurn(role="user", content="What is the holiday carryover?"),
        ]
    )

    assert terms.split() == ["holiday", "policy", "carryover"]


def test_an_empty_history_borrows_nothing() -> None:
    assert _context_terms([]) == ""


# --------------------------------------------------------------------------
# the routing edge
# --------------------------------------------------------------------------


def test_anything_retrieved_goes_straight_to_generate() -> None:
    state: AgentState = {"retrieved": [{"score": 0.1}], "attempts": 1}

    assert _enough(state) == GENERATE


def test_nothing_retrieved_rewrites_while_attempts_remain() -> None:
    state: AgentState = {"retrieved": [], "attempts": 1}

    assert _enough(state) == REWRITE


def test_the_cycle_is_bounded() -> None:
    """A conditional edge routing backwards is a cycle, and an unbounded cycle is
    a graph that can bill indefinitely."""
    state: AgentState = {"retrieved": [], "attempts": MAX_ATTEMPTS}

    assert _enough(state) == GENERATE
