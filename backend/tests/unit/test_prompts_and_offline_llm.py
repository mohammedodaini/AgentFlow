"""The prompt templates, the loader, and the offline model (M7).

These three are tested together on purpose, because they are coupled and the
coupling is the point. `app/rag/context.py` writes `[n]` blocks, the template
embeds them, and `app/llm/offline.py` parses them back out. Any one could be
changed alone without breaking a test that looked only at it — and the product
would then answer every question with a refusal, silently.

So these tests render the *real* templates rather than fixtures. If a template
is reworded in a way the parser cannot follow, this file fails, which is exactly
the alarm the coupling needs.
"""

from __future__ import annotations

import pytest

from app.llm.offline import NO_CONTEXT_ANSWER, OfflineLLM
from app.prompts import loader as prompts
from app.rag.context import assemble_context
from app.rag.generation import ANSWER_PROMPT, SYSTEM_PROMPT
from tests.unit.test_context import EXPENSES, HOLIDAY, PLANTS, chunk


@pytest.fixture(autouse=True)
def _fresh_prompt_cache() -> None:
    """`load_prompt` caches for the life of the process, which is right in
    production and wrong across tests that assert on cache behaviour."""
    prompts.load_prompt.cache_clear()


def rendered(question: str, contents: list[str]) -> str:
    """The real prompt, built the way `Generator` builds it."""
    context = assemble_context([chunk(content) for content in contents], budget=1000)
    return prompts.render(ANSWER_PROMPT, context=context.text, question=question)


# --------------------------------------------------------------------------
# the loader
# --------------------------------------------------------------------------


def test_the_shipped_prompts_load() -> None:
    """Templates are packaged with the code, not configuration mounted beside
    it. If this fails in a container, the image is missing its `.md` files."""
    assert prompts.load_prompt(SYSTEM_PROMPT)
    assert "{context}" in prompts.load_prompt(ANSWER_PROMPT)


def test_a_missing_placeholder_raises() -> None:
    """The single most important behaviour in the loader.

    The tempting fix, the first time this fires in production, is a defaulting
    formatter that substitutes `""`. Then a prompt renders with an empty context
    block, the model answers from training data, cites nothing, and looks
    entirely normal. `KeyError` is the cheapest possible version of that bug.
    """
    with pytest.raises(KeyError):
        prompts.render(ANSWER_PROMPT, context="something")


def test_an_unknown_prompt_names_the_ones_that_exist() -> None:
    """A typo should be visible in the error, not send someone to check
    deployment paths."""
    with pytest.raises(prompts.PromptNotFoundError, match=SYSTEM_PROMPT):
        prompts.load_prompt("rag/nonexistent")


def test_the_loader_caches() -> None:
    """A filesystem hit in the hot path of every answer, avoided."""
    prompts.load_prompt(SYSTEM_PROMPT)
    prompts.load_prompt(SYSTEM_PROMPT)

    assert prompts.load_prompt.cache_info().hits >= 1


# --------------------------------------------------------------------------
# the templates' own content
# --------------------------------------------------------------------------


def test_the_system_prompt_and_the_offline_refusal_are_the_same_sentence() -> None:
    """Two files that must agree, byte for byte.

    The template tells Claude to refuse with an exact sentence; `OfflineLLM`
    returns that sentence directly. If they drift, a test asserting the refusal
    passes offline while the real product says something different — and no test
    with a real key would notice, because the string would still be plausible.
    """
    assert NO_CONTEXT_ANSWER in prompts.load_prompt(SYSTEM_PROMPT)


def test_the_system_prompt_forbids_outside_knowledge() -> None:
    """The grounding instruction is the whole reason this is a file and not a
    string literal. Asserting on its substance is crude, and it is the only
    protection against someone trimming the rule that stops invention."""
    system = prompts.load_prompt(SYSTEM_PROMPT).lower()

    assert "only what the sources say" in system
    assert "cite" in system


def test_the_question_is_last_in_the_rendered_prompt() -> None:
    """Instructions nearest the end are followed most reliably, and
    `OfflineLLM` reads the last line to learn what was asked. Both break
    quietly if the template is reordered."""
    prompt = rendered("How do I claim expenses?", [EXPENSES])

    assert prompt.strip().endswith("How do I claim expenses?")


# --------------------------------------------------------------------------
# the offline model
# --------------------------------------------------------------------------


async def test_it_quotes_the_sentence_that_answers_the_question() -> None:
    """The property that makes every other M7 test meaningful.

    A canned "This is a test answer." would let the context be empty, the
    citations wrong and the budget broken while every assertion still passed.
    This provider *cannot* answer unless the right chunk was retrieved and
    survived into the prompt.
    """
    completion = await OfflineLLM().complete(
        system=prompts.load_prompt(SYSTEM_PROMPT),
        prompt=rendered("expenses receipt reimbursed", [PLANTS, EXPENSES, HOLIDAY]),
    )

    assert "reimbursed" in completion.text


async def test_the_answer_carries_the_citation_marker_of_its_source() -> None:
    """The parser and `context.py`'s `[n]` format, meeting in the middle."""
    completion = await OfflineLLM().complete(
        system="", prompt=rendered("holiday manager approval", [EXPENSES, HOLIDAY])
    )

    assert completion.text.endswith("[2]"), completion.text


async def test_the_answer_never_parrots_the_question_back() -> None:
    """A regression test, and the bug it pins produced perfect nonsense.

    The block regex originally ran to the end of the string, so the *last*
    context block swallowed the trailing `Question:` line. That line then scored
    a perfect word-overlap match against itself and won every time — so the
    "answer" was the question, repeated, with a citation attached. Fluent,
    well-formed, and completely wrong.

    It survived the unit tests here because a question's words also appear in
    the chunk that answers it, so "the answer contains 'reimbursed'" passed
    either way. Only an integration test with a question whose wording differed
    from the passage exposed it. The assertion is therefore on the *shape* of
    the failure — the answer must not simply be the question.
    """
    question = "What is the reimbursement process for travel?"

    completion = await OfflineLLM().complete(
        system="", prompt=rendered(question, [EXPENSES, HOLIDAY, PLANTS])
    )

    assert question not in completion.text
    assert "Question:" not in completion.text
    assert completion.text.startswith("Expenses are reimbursed"), completion.text


async def test_an_empty_context_produces_the_refusal() -> None:
    """The path that must never reach a real model. `Generator` refuses before
    calling out at all; this is the belt to that braces."""
    completion = await OfflineLLM().complete(system="", prompt="Question: anything at all")

    assert completion.text == NO_CONTEXT_ANSWER


async def test_streaming_yields_the_same_answer_in_pieces() -> None:
    """Reassembled chunks must equal the whole. A provider that streamed a
    single chunk would leave the event framing and client reassembly
    untested."""
    llm = OfflineLLM()
    prompt = rendered("expenses receipt", [EXPENSES, HOLIDAY])

    streamed = "".join([piece async for piece in llm.stream(system="", prompt=prompt)])
    whole = await llm.complete(system="", prompt=prompt)

    assert streamed == whole.text
    assert len([piece async for piece in llm.stream(system="", prompt=prompt)]) > 1


async def test_it_is_deterministic() -> None:
    """M8's evaluation compares answers across prompt changes. A provider that
    varied would make every comparison measure sampling noise."""
    llm = OfflineLLM()
    prompt = rendered("expenses", [EXPENSES, PLANTS])

    first = await llm.complete(system="", prompt=prompt)
    second = await llm.complete(system="", prompt=prompt)

    assert first.text == second.text


async def test_it_reports_usage_and_a_stop_reason() -> None:
    """Approximate, and present. The `Completion` contract is what M12 bills on,
    so a provider returning nothing there would make cost accounting untestable
    offline."""
    completion = await OfflineLLM().complete(system="sys", prompt=rendered("expenses", [EXPENSES]))

    assert completion.input_tokens > 0
    assert completion.output_tokens > 0
    assert not completion.was_truncated
